package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.Event;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.xivsupport.callouts.ModifiedCalloutRepository;
import gg.xp.xivsupport.events.ACTLogLineEvent;
import gg.xp.xivsupport.events.actlines.events.*;
import gg.xp.xivsupport.events.misc.pulls.PullEndedEvent;
import gg.xp.xivsupport.events.misc.pulls.PullStartedEvent;
import gg.xp.xivsupport.events.triggers.seq.SequentialTrigger;
import gg.xp.xivsupport.events.triggers.seq.SequentialTriggerFailedEvent;
import gg.xp.xivsupport.models.XivCombatant;
import gg.xp.xivsupport.replay.PullRecovery;
import gg.xp.xivsupport.speech.CalloutEvent;
import gg.xp.xivsupport.speech.TtsRequest;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.events.triggers.marks.*;
import gg.xp.xivsupport.events.triggers.marks.adv.*;
import gg.xp.telestosupport.*;

import java.lang.reflect.Field;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

/** Diagnostic records contain code locations and game data, never event text. */
final class EngineDiagnostics {
    private final PullRecovery recovery;
    private final Consumer<Map<String, Object>> sink;
    private final Map<String, AtomicLong> counts = new LinkedHashMap<>();
    private final Map<String, Field> sequences = new ConcurrentHashMap<>();
    private final Map<Long, Long> actors = new LinkedHashMap<>();
    private long nextActor;
    private Instant firstEvent;
    private final Map<Event, Long> marks = Collections.synchronizedMap(new WeakHashMap<>());
    private final AtomicLong markSequence = new AtomicLong();
    private AutoMarkServiceSelector markerService;
    private XivState state;
    private TelestoPartyListHandler party;

    EngineDiagnostics(PullRecovery recovery, Consumer<Map<String, Object>> sink) {
        this.recovery = recovery;
        this.sink = sink;
        for (String key : List.of("raw", "casts", "abilities", "buffs", "callouts", "tts", "failures")) {
            counts.put(key, new AtomicLong());
        }
    }

    void install(EventDistributor dist) {
        dist.registerHandler(ACTLogLineEvent.class, (c, e) -> count("raw"));
        dist.registerHandler(AbilityCastStart.class, (c, e) -> count("casts"));
        dist.registerHandler(AbilityUsedEvent.class, (c, e) -> count("abilities"));
        dist.registerHandler(BuffApplied.class, (c, e) -> count("buffs"));
        dist.registerHandler(CalloutEvent.class, (c, e) -> count("callouts"));
        dist.registerHandler(TtsRequest.class, (c, e) -> count("tts"));
        dist.registerHandler(SequentialTriggerFailedEvent.class, (c, e) -> safely(() -> failure(e)));
        dist.registerHandler(BaseEvent.class, (c, e) -> safely(() -> mechanic(e)));
        dist.registerHandler(BaseEvent.class, (c, e) -> safely(() -> markRequest(e)));
        dist.registerHandler(BaseTelestoResponse.class, (c, e) -> safely(() -> markResponse(e)));
        emit("engine_runtime", Map.of("java_version", System.getProperty("java.version", ""),
                "heap_max", Runtime.getRuntime().maxMemory()));
        var timer = Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(task, "NyaaDiagnostics");
            thread.setDaemon(true);
            return thread;
        });
        timer.scheduleAtFixedRate(this::pipeline, 10, 10, TimeUnit.SECONDS);
    }

    void configureAutomark(AutoMarkServiceSelector selector, XivState state, TelestoPartyListHandler party) {
        markerService = selector;
        this.state = state;
        this.party = party;
    }

    void automarkConfig(boolean enabled, boolean available, boolean nativeUmad) {
        emit("engine_automark_config", Map.of("enabled", enabled, "available", available,
                "transport", enabled && available ? "telesto" : "none", "native_umad", nativeUmad));
    }

    private static Event markRoot(Event event) {
        Event result = null;
        for (int depth = 0; event != null && depth < 16; depth++, event = event.getParent()) {
            if (event instanceof AutoMarkRequest || event instanceof SpecificAutoMarkRequest) return event;
            if (event instanceof ClearAutoMarkRequest || event instanceof AutoMarkSlotRequest
                    || event instanceof SpecificAutoMarkSlotRequest) result = event;
        }
        return result;
    }

    private Map<String, Object> markData(Event root, Event event) {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("seq", marks.computeIfAbsent(root, ignored -> markSequence.incrementAndGet()));
        MarkerSign marker = root instanceof SpecificAutoMarkRequest request ? request.getMarker()
                : root instanceof SpecificAutoMarkSlotRequest request ? request.getMarker()
                : root instanceof ClearAutoMarkRequest ? MarkerSign.CLEAR : MarkerSign.ATTACK_NEXT;
        data.put("marker", marker.name());
        if (root instanceof HasTargetEntity target) data.put("target_actor", actor(target.getTarget()));
        for (int depth = 0; event != null && depth < 16; depth++, event = event.getParent()) {
            if (event instanceof AutoMarkSlotRequest request) data.put("slot", request.getSlotToMark());
            if (event instanceof SpecificAutoMarkSlotRequest request) data.put("slot", request.getSlotToMark());
        }
        return data;
    }

    private void markRequest(BaseEvent event) {
        if (recovery != null && recovery.clock.replaying()) return;
        if (markRoot(event) != event) return;
        Map<String, Object> data = markData(event, event);
        data.put("stage", "requested");
        if (recovery != null && !recovery.outputAllowed()) data.put("reason", "stale");
        else if (markerService == null || markerService.getOptions().stream()
                .noneMatch(handle -> handle.descriptor().id().equals("telesto-am"))) data.put("reason", "unavailable");
        else if (markerService.getEffectiveOption().descriptor().id().equals("none")) data.put("reason", "disabled");
        else if (event instanceof HasTargetEntity target && state != null) {
            int slot = state.getPartySlotOf(target.getTarget()) + 1;
            if (slot <= 0 || party == null || !party.matchesSlot(target.getTarget().getId(), slot)) {
                data.put("reason", "unresolved");
            }
        }
        emit("engine_automark", data);
    }

    private void markResponse(BaseTelestoResponse event) {
        TelestoOutgoingMessage outgoing = event.getResponseTo();
        Event root = outgoing == null ? null : markRoot(outgoing);
        if (root == null) return;
        Map<String, Object> data = markData(root, outgoing);
        if (event instanceof TelestoHttpError error) {
            data.put("stage", "rejected");
            data.put("reason", "http_error");
            data.put("http_status", error.getResponse().statusCode());
        }
        else if (event instanceof TelestoConnectionError error) {
            data.put("stage", "failed");
            data.put("reason", error.getError() instanceof java.util.concurrent.TimeoutException ? "stale"
                    : error.getError() instanceof IllegalArgumentException
                    || error.getError() instanceof tools.jackson.core.JacksonException ? "invalid_response" : "connection_error");
        }
        else {
            data.put("stage", "accepted");
            data.put("http_status", 200);
        }
        emit("engine_automark", data);
    }

    void registerSequences(ModifiedCalloutRepository repo) {
        for (var group : repo.getAllCallouts()) {
            for (var call : group.getCallouts()) {
                Field callField = call.getField();
                if (callField == null || !bundled(callField.getDeclaringClass())) continue;
                Class<?> type = callField.getDeclaringClass();
                for (Field field : type.getDeclaredFields()) {
                    if (SequentialTrigger.class.isAssignableFrom(field.getType())) {
                        sequences.put(type.getSimpleName() + '.' + field.getName(), field);
                    }
                }
            }
        }
    }

    private void count(String key) {
        counts.get(key).incrementAndGet();
    }

    void pipeline() {
        Map<String, Object> totals = new LinkedHashMap<>();
        counts.forEach((key, value) -> totals.put(key, value.get()));
        emit("engine_pipeline", Map.of("counters", totals));
    }

    void callout(Field field, long seq, boolean ttsOnly, boolean hasText, boolean hasTts, boolean expired) {
        safely(() -> describeCallout(field, seq, ttsOnly, hasText, hasTts, expired));
    }

    private void describeCallout(Field field, long seq, boolean ttsOnly, boolean hasText, boolean hasTts, boolean expired) {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("seq", seq);
        data.put("tts_only", ttsOnly);
        data.put("has_text", hasText);
        data.put("has_tts", hasTts);
        data.put("expired", expired);
        codeField(data, field);
        emit("engine_callout", data);
    }

    private static void codeField(Map<String, Object> data, Field field) {
        if (field != null && bundled(field.getDeclaringClass())) {
            data.put("trigger_class", field.getDeclaringClass().getName());
            data.put("trigger_field", field.getName());
        }
    }

    private void failure(SequentialTriggerFailedEvent event) {
        count("failures");
        Map<String, Object> data = errorData(event.getError());
        if (event.getTriggerName() != null) codeField(data, sequences.get(event.getTriggerName()));
        if (bundled(event.getInitialEvent().getClass())) {
            data.put("initial_event", event.getInitialEvent().getClass().getName());
        }
        String wait = Objects.requireNonNullElse(event.getPendingWait(), "");
        String kind = "unknown";
        if (wait.matches("waitMs [0-9]{1,12}")) {
            kind = "time";
            data.put("wait_ms", Long.parseLong(wait.substring(7)));
        }
        else {
            String[] parts = wait.split(" ");
            Class<?> waited = codeClass("gg.xp.xivsupport.events.actlines.events." + parts[0]);
            if (waited != null && BaseEvent.class.isAssignableFrom(waited)) {
                data.put("wait_event", waited.getName());
                kind = wait.endsWith(" in quick succession") ? "burst"
                        : wait.contains(" until ") ? "until" : "event";
            }
        }
        data.put("wait_kind", kind);
        emit("sequence_failed", data);
    }

    void error(String site, Throwable error) {
        safely(() -> {
            Map<String, Object> data = errorData(error);
            data.put("site", site);
            emit("engine_error", data);
        });
    }

    private static Map<String, Object> errorData(Throwable error) {
        Map<String, Object> data = new LinkedHashMap<>();
        List<Map<String, Object>> frames = new ArrayList<>();
        Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<>());
        for (Throwable cause = error; cause != null && seen.size() < 4 && seen.add(cause); cause = cause.getCause()) {
            Class<?> type = cause.getClass();
            if (bundled(type) || (type.getClassLoader() == null && type.getName().startsWith("java."))) {
                data.putIfAbsent("error_type", type.getName());
            }
            for (StackTraceElement frame : cause.getStackTrace()) {
                Class<?> frameClass = codeClass(frame.getClassName());
                if (frameClass == null || frames.size() >= 24) continue;
                boolean declared = Arrays.stream(frameClass.getDeclaredMethods())
                        .anyMatch(method -> method.getName().equals(frame.getMethodName()));
                if (declared || frame.getMethodName().equals("<init>")) {
                    frames.add(Map.of("class", frameClass.getName(), "method", frame.getMethodName(),
                            "line", frame.getLineNumber()));
                }
            }
        }
        data.put("frames", frames);
        return data;
    }

    private static boolean bundled(Class<?> type) {
        return type.getName().startsWith("gg.xp.")
                && Objects.equals(type.getProtectionDomain().getCodeSource(),
                                  EngineDiagnostics.class.getProtectionDomain().getCodeSource());
    }

    private static Class<?> codeClass(String name) {
        if (!name.startsWith("gg.xp.") || name.length() > 240) return null;
        try {
            Class<?> type = Class.forName(name, false, EngineDiagnostics.class.getClassLoader());
            return bundled(type) ? type : null;
        }
        catch (ClassNotFoundException | LinkageError ignored) {
            return null;
        }
    }

    private synchronized long actor(XivCombatant combatant) {
        if (combatant == null || combatant.getId() == 0 || combatant.getId() == 0xE0000000L) return 0;
        if (actors.size() >= 4096) actors.clear();
        return actors.computeIfAbsent(combatant.getId(), ignored -> ++nextActor);
    }

    private void mechanic(BaseEvent event) {
        if (recovery != null && recovery.clock.replaying()) return;
        Map<String, Object> data = new LinkedHashMap<>();
        if (event instanceof AbilityCastStart cast) {
            data.put("event_kind", "cast");
            data.put("game_id", cast.getAbility().getId());
            data.put("duration_ms", cast.getInitialDuration().toMillis());
        }
        else if (event instanceof BuffApplied buff) {
            data.put("event_kind", "buff_add");
            data.put("game_id", buff.getBuff().getId());
            data.put("duration_ms", buff.getInitialDuration().toMillis());
            data.put("stacks", buff.getRawStacks());
        }
        else if (event instanceof BuffRemoved buff) {
            data.put("event_kind", "buff_remove");
            data.put("game_id", buff.getBuff().getId());
            data.put("stacks", buff.getRawStacks());
        }
        else if (event instanceof TetherEvent tether) {
            data.put("event_kind", "tether");
            data.put("game_id", tether.getId());
        }
        else if (event instanceof HeadMarkerEvent marker) {
            data.put("event_kind", "headmarker");
            data.put("game_id", marker.getMarkerId());
        }
        else if (event instanceof EntityKilledEvent) data.put("event_kind", "death");
        else if (event instanceof ZoneChangeEvent zone) {
            data.put("event_kind", "zone");
            data.put("zone_id", zone.getZone().getId());
        }
        else if (event instanceof PullStartedEvent) data.put("event_kind", "pull_start");
        else if (event instanceof PullEndedEvent) data.put("event_kind", "pull_end");
        else return;
        if (event instanceof HasSourceEntity source && source.getSource() != null) {
            data.put("source_actor", actor(source.getSource()));
            data.put("source_player", source.getSource().isThePlayer());
        }
        if (event instanceof HasTargetEntity target && target.getTarget() != null) {
            data.put("target_actor", actor(target.getTarget()));
            data.put("on_player", target.getTarget().isThePlayer());
        }
        synchronized (this) {
            if (firstEvent == null) firstEvent = event.getEffectiveHappenedAt();
            data.put("event_ms", Duration.between(firstEvent, event.getEffectiveHappenedAt()).toMillis());
        }
        emit("engine_event", data);
    }

    private void emit(String event, Map<String, Object> fields) {
        safely(() -> {
            Map<String, Object> record = new LinkedHashMap<>(fields);
            record.put("t", "diagnostic");
            record.put("event", event);
            record.put("replaying", recovery != null && recovery.clock.replaying());
            sink.accept(record);
        });
    }

    private static void safely(Runnable action) {
        try {
            action.run();
        }
        catch (RuntimeException | LinkageError ignored) {
            // Diagnostics must not interrupt the engine.
        }
    }
}
