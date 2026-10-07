package gg.xp.nyaa;

import gg.xp.reevent.events.BasicEventQueue;
import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.EventContext;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.reevent.events.InitEvent;
import gg.xp.reevent.scan.AutoHandlerConfig;
import gg.xp.xivsupport.callouts.CalloutGroup;
import gg.xp.xivsupport.callouts.CalloutTrackingKey;
import gg.xp.xivsupport.callouts.ModifiedCalloutHandle;
import gg.xp.xivsupport.callouts.ModifiedCalloutRepository;
import gg.xp.xivsupport.callouts.RawModifiedCallout;
import gg.xp.xivsupport.events.ACTLogLineEvent;
import gg.xp.xivsupport.events.actlines.events.AbilityCastStart;
import gg.xp.xivsupport.events.actlines.events.AbilityUsedEvent;
import gg.xp.xivsupport.events.actlines.events.BuffApplied;
import gg.xp.xivsupport.events.actlines.events.WipeEvent;
import gg.xp.xivsupport.events.actlines.events.ZoneChangeEvent;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.DutyCommenceEvent;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.FadeOutEvent;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.VictoryEvent;
import gg.xp.xivsupport.events.misc.pulls.PullEndedEvent;
import gg.xp.xivsupport.events.misc.pulls.PullStartedEvent;
import gg.xp.xivsupport.events.ws.ActWsRawMsg;
import gg.xp.xivsupport.events.state.RefreshCombatantsRequest;
import gg.xp.xivsupport.events.state.RefreshSpecificCombatantsRequest;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.events.state.combatstate.StatusEffectRepository;
import gg.xp.xivsupport.replay.PullRecovery;
import gg.xp.xivsupport.replay.RecoveryClock;
import gg.xp.xivsupport.replay.RecoveryQueue;
import gg.xp.xivsupport.persistence.UserDirPropsPersistenceProvider;
import gg.xp.xivsupport.speech.CalloutEvent;
import gg.xp.xivsupport.speech.CalloutTraceInfo;
import gg.xp.xivsupport.speech.ModifiableCalloutTraceInfo;
import gg.xp.xivsupport.speech.TtsRequest;
import gg.xp.xivsupport.sys.KnownLogSource;
import gg.xp.xivsupport.sys.PrimaryLogSource;
import gg.xp.xivsupport.sys.XivMain;
import gg.xp.xivsupport.events.triggers.marks.adv.AutoMarkServiceSelector;
import gg.xp.services.ServiceHandle;
import gg.xp.telestosupport.TelestoMain;
import gg.xp.telestosupport.TelestoStatusUpdatedEvent;
import gg.xp.telestosupport.TelestoPartyListHandler;
import gg.xp.telestosupport.TelestoHttpError;
import gg.xp.telestosupport.TelestoConnectionError;
import gg.xp.telestosupport.BaseTelestoResponse;
import gg.xp.xivsupport.triggers.ultimate.DMU;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import org.picocontainer.MutablePicoContainer;

import java.awt.Color;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.time.Instant;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicLong;

public final class TriggeventCore {

    // Bundled JDK 17 may otherwise use a Windows code page.
    private static final PrintStream OUT = new PrintStream(System.out, true, StandardCharsets.UTF_8);

    private static final boolean DIAG_ON = System.getenv("NYAA_TV_DIAG") != null;
    private static final Map<String, AtomicLong> DIAG = new LinkedHashMap<>();

    private static final Map<String, ModifiedCalloutHandle> CALLOUTS = new HashMap<>();
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final AtomicLong CALLOUT_SEQ = new AtomicLong();
    private static final int MAX_PENDING_SPEECH = 1024;
    private static final Map<CalloutTrackingKey, PendingSpeech> PENDING_SPEECH = new LinkedHashMap<>();
    private static long speechEpoch;
    private record PendingSpeech(long epoch, String id,
                                 ModifiedCalloutHandle handle, String template) {}

    private static volatile TelestoMain TELESTO;
    private static volatile AutoMarkServiceSelector AM_SELECTOR;
    private static DMU DMU_TRIGGERS;
    private static NativeAutomarkers NATIVE_AUTOMARKERS;
    private static boolean nativeUmadEnabled = true;
    private static PullRecovery RECOVERY;
    private static EventMaster MASTER;
    private static EngineDiagnostics diagnostics;
    private static boolean requestedAutomark;
    private static JsonNode automarkCommand;
    private static String recoveryStatus = "state_only";
    private static String recoveryReason = "";

    private static AtomicLong diagCount(String key) {
        synchronized (DIAG) {
            return DIAG.computeIfAbsent(key, k -> new AtomicLong());
        }
    }

    private TriggeventCore() {
    }

    public static void main(String[] args) throws Exception {
        // Swing startup needs a display. The bridge supplies Xvfb when available.

        try {
            final MutablePicoContainer pico = bootEngine();
            final EventMaster master = pico.getComponent(EventMaster.class);

            emitStatus(true, "Triggevent Engine ready");
            diag("ready; reading WS messages on stdin; recovery=1; history=1; catchup=1; custom=1; speech_cancel=1; speech_cancel_all=1; diagnostics=1");

            try {
                pico.getComponent(BasicEventQueue.class).waitDrain();
                emitInventory(pico);
            } catch (Throwable t) {
                diag("inventory error: " + t);
                diagnosticError("inventory", t);
            }

            final BufferedReader in =
                    new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                try {
                    final JsonNode frame = MAPPER.readTree(line);
                    if (frame != null && frame.isObject() && frame.has("nyaa_cmd")) {
                        handleCommand(frame);
                        continue;
                    }
                    if (frame != null && frame.isObject()) {
                        RECOVERY.feed(line, frame);
                    }
                } catch (Throwable t) {
                    if (RECOVERY.clock.replaying()) {
                        RECOVERY.recordFailure();
                        if ("complete".equals(recoveryStatus) || "state_only".equals(recoveryStatus)) {
                            recoveryStatus = "degraded";
                        }
                    }
                    diag("feed error: " + t);
                    diagnosticError("feed", t);
                }
            }
            try {
                final BasicEventQueue q = pico.getComponent(BasicEventQueue.class);
                q.waitDrain();
                for (int i = 0; i < 4; i++) {
                    Thread.sleep(120);
                    q.waitDrain();
                }
            } catch (Throwable ignored) {
            }
            if (DIAG_ON) {
                final StringBuilder sb = new StringBuilder("event counts:");
                synchronized (DIAG) {
                    DIAG.forEach((k, v) -> sb.append(' ').append(k).append('=').append(v.get()));
                }
                diag(sb.toString());
            }
            emitStatus(false, "stdin closed");
            diagnostics.pipeline();
        } catch (Throwable t) {
            t.printStackTrace();
            diagnosticError("boot", t);
            OUT.flush();
            System.exit(1);
        } finally {
            // Engine threads would keep the JVM alive after stdin closes.
            OUT.flush();
            System.exit(0);
        }
    }

    // Bootstrap without a live ACT connection or writes to user settings.
    private static MutablePicoContainer bootEngine() throws Exception {
        final Method requiredComponents = XivMain.class.getDeclaredMethod("requiredComponents");
        requiredComponents.setAccessible(true);
        final MutablePicoContainer pico =
                (MutablePicoContainer) requiredComponents.invoke(null);

        final RecoveryClock clock = new RecoveryClock();
        final RecoveryQueue queue = new RecoveryQueue(clock);
        pico.removeComponent(BasicEventQueue.class);
        pico.addComponent(BasicEventQueue.class, queue);
        pico.addComponent(clock);

        pico.addComponent(UserDirPropsPersistenceProvider.inUserDataFolder("triggevent", true));

        // Replay parsers reconstruct player, zone and party state.
        pico.getComponent(AutoHandlerConfig.class).setNotLive(true);

        // Live source enables Telesto and party polling without opening an ACT WebSocket.
        pico.getComponent(PrimaryLogSource.class).setLogSource(KnownLogSource.WEBSOCKET_LIVE);

        final EventDistributor dist = pico.getComponent(EventDistributor.class);
        RECOVERY = new PullRecovery(clock, queue, pico.getComponent(EventMaster.class),
                pico.getComponent(PrimaryLogSource.class));
        dist.registerHandler(RECOVERY);
        MASTER = pico.getComponent(EventMaster.class);
        diagnostics = new EngineDiagnostics(RECOVERY,
                record -> println(MAPPER.writeValueAsString(record)));
        diagnostics.install(dist);
        dist.registerHandler(BaseEvent.class, TriggeventCore::onSpeechBoundary);
        dist.registerHandler(CalloutEvent.class, TriggeventCore::onCallout);
        dist.registerHandler(TtsRequest.class, TriggeventCore::onTtsRequest);
        dist.registerHandler(TelestoStatusUpdatedEvent.class, TriggeventCore::onTelestoStatus);
        dist.registerHandler(TelestoHttpError.class, (c, e) -> onAutomarkFailure(e));
        dist.registerHandler(TelestoConnectionError.class, (c, e) -> onAutomarkFailure(e));
        dist.registerHandler(RefreshCombatantsRequest.class, (c, e) -> requestCombatants(List.of()));
        dist.registerHandler(RefreshSpecificCombatantsRequest.class, (c, e) -> requestCombatants(e.getCombatants()));

        if (DIAG_ON) {
            dist.registerHandler(ACTLogLineEvent.class, (c, e) -> diagCount("ACTLogLineEvent").incrementAndGet());
            dist.registerHandler(AbilityCastStart.class, (c, e) -> diagCount("AbilityCastStart").incrementAndGet());
            dist.registerHandler(AbilityUsedEvent.class, (c, e) -> diagCount("AbilityUsedEvent").incrementAndGet());
            dist.registerHandler(BuffApplied.class, (c, e) -> diagCount("BuffApplied").incrementAndGet());
            dist.registerHandler(RawModifiedCallout.class, (c, e) -> diagCount("RawModifiedCallout").incrementAndGet());
            dist.registerHandler(CalloutEvent.class, (c, e) -> diagCount("CalloutEvent").incrementAndGet());
            dist.registerHandler(TtsRequest.class, (c, e) -> diagCount("TtsRequest").incrementAndGet());
        }

        dist.acceptEvent(new InitEvent());                 // Runs startup Groovy scripts.
        dist.registerHandler(BaseEvent.class, new CustomTriggers(pico.getComponent(XivState.class),
                pico.getComponent(StatusEffectRepository.class)));
        pico.getComponent(EventMaster.class).start();

        try {
            TELESTO = pico.getComponent(TelestoMain.class);
            if (TELESTO != null) {
                TELESTO.setOutgoingGate(message -> RECOVERY.outputAllowed());
                TELESTO.setDeliveryPermit(RECOVERY::outputPermit);
                TELESTO.setDeliveryFailure(RECOVERY::cancelPendingOutput);
            }
            AM_SELECTOR = pico.getComponent(AutoMarkServiceSelector.class);
            DMU_TRIGGERS = pico.getComponent(DMU.class);
            diagnostics.configureAutomark(AM_SELECTOR, pico.getComponent(XivState.class),
                    pico.getComponent(TelestoPartyListHandler.class));
            final String envUri = System.getenv("NYAA_TELESTO_URI");
            if (envUri != null && !envUri.isBlank() && TELESTO != null) {
                trySet(() -> TELESTO.getUriSetting().set(URI.create(envUri.trim())));
            }
            // Default to no marker service. Keyboard handlers could send keys to the focused window.
            requestedAutomark = "1".equals(System.getenv("NYAA_AUTOMARK"));
            applyAutomark(requestedAutomark);
            NATIVE_AUTOMARKERS = new NativeAutomarkers(pico);
            if (AM_SELECTOR != null) {
                final boolean hasTelesto = AM_SELECTOR.getOptions().stream()
                        .anyMatch(hh -> "telesto-am".equals(hh.descriptor().id()));
                diag("automark: telesto-am " + (hasTelesto ? "available" : "MISSING")
                        + ", selected=" + AM_SELECTOR.getEffectiveOption().descriptor().id()
                        + ", telesto=" + (TELESTO != null ? "ok" : "null"));
            } else {
                diag("automark: AutoMarkServiceSelector MISSING (telesto-core not scanned?)");
            }
        } catch (Throwable t) {
            diag("telesto wiring skipped: " + t);
            diagnosticError("automark", t);
        }
        return pico;
    }

    private static void onCallout(EventContext ctx, CalloutEvent ev) {
        try {
            boolean delayed = ev.getTtsDelayMs() > 0 && ev.getCallText() != null
                    && !ev.getCallText().isBlank();
            synchronized (PENDING_SPEECH) {
                if (ev.replaces() != null) {
                    PENDING_SPEECH.remove(ev.replaces().trackingKey());
                }
                if (delayed) {
                    String id = calloutId(ev);
                    ModifiedCalloutHandle handle = CALLOUTS.get(id);
                    PENDING_SPEECH.put(ev.trackingKey(), new PendingSpeech(speechEpoch, id,
                            handle, handle == null ? null : handle.getEffectiveTts()));
                    if (PENDING_SPEECH.size() > MAX_PENDING_SPEECH) {
                        PENDING_SPEECH.remove(PENDING_SPEECH.keySet().iterator().next());
                        diag("delayed speech queue full; oldest callout dropped");
                    }
                }
                if (!RECOVERY.outputAllowed()) {
                    return;
                }
                String text = ev.getVisualText();
                if (delayed && (text == null || text.isBlank())) {
                    text = ev.getCallText();
                }
                emitCallout(ev, delayed ? null : ev.getCallText(), text, false,
                        ev.getEffectiveHappenedAt());
            }
        } catch (Throwable t) {
            diag("emit error: " + t);
            diagnosticError("callout", t);
        }
    }

    private static void onTtsRequest(EventContext ctx, TtsRequest request) {
        if (!(request.getParent() instanceof CalloutEvent callout)) {
            return;
        }
        synchronized (PENDING_SPEECH) {
            PendingSpeech pending = PENDING_SPEECH.remove(callout.trackingKey());
            if (pending == null) {
                return;
            }
            try {
                if (pending.epoch() != speechEpoch || !RECOVERY.outputAllowed()) {
                    return;
                }
                ModifiedCalloutHandle handle = pending.handle();
                if (handle != null && (!handle.isTtsEffectivelyEnabled()
                        || !Objects.equals(pending.template(), handle.getEffectiveTts()))) {
                    return;
                }
                emitCallout(callout, request.getTtsString(), null, true, RECOVERY.clock.now());
            } catch (Throwable t) {
                diag("delayed speech error: " + t);
                diagnosticError("delayed_speech", t);
            }
        }
    }

    private static void onSpeechBoundary(EventContext ctx, BaseEvent event) {
        if (event instanceof ZoneChangeEvent || event instanceof WipeEvent
                || event instanceof FadeOutEvent || event instanceof VictoryEvent
                || event instanceof PullEndedEvent || event instanceof DutyCommenceEvent
                || event instanceof PullStartedEvent) {
            cancelPendingSpeech();
        }
    }

    private static void cancelPendingSpeech() {
        synchronized (PENDING_SPEECH) {
            speechEpoch++;
            PENDING_SPEECH.clear();
        }
    }

    private static void emitCallout(CalloutEvent ev, String tts, String text,
                                    boolean ttsOnly, Instant at) {
        try {
            Map<String, Object> message = new LinkedHashMap<>();
            message.put("t", "callout");
            long seq = CALLOUT_SEQ.incrementAndGet();
            message.put("seq", seq);
            Field sourceField = calloutField(ev);
            message.put("id", ev instanceof CustomTriggers.Callout custom ? custom.id : idForField(sourceField));
            message.put("tts", tts);
            message.put("text", text);
            if (ttsOnly) {
                message.put("tts_only", true);
            }
            message.put("at", at.toEpochMilli());
            message.put("severity", severity(ev.getColorOverride()));
            if (!ttsOnly) {
                message.put("sound", ev.getSound());
            }
            boolean expired = ev.isExpired();
            message.put("expired", expired);
            message.values().removeIf(Objects::isNull);
            diagnostics.callout(sourceField, seq, ttsOnly, text != null && !text.isBlank(),
                    tts != null && !tts.isBlank(), expired);
            println(MAPPER.writeValueAsString(message));
            // Report PrintStream write failures once per failure streak.
            if (OUT.checkError()) {
                diag("stdout write failed while emitting a callout");
            }
        } catch (Throwable t) {
            diag("emit error: " + t);
            diagnosticError("callout", t);
        }
    }

    // Telesto status requests require the live source setting.
    private static void onTelestoStatus(EventContext ctx, TelestoStatusUpdatedEvent ev) {
        try {
            final String s = switch (ev.getNewStatus()) {
                case GOOD -> "good";
                case BAD -> "bad";
                default -> "unknown";
            };
            println("{\"t\":\"telesto\",\"status\":\"" + s + "\"}");
        } catch (Throwable t) {
            diag("telesto status emit error: " + t);
        }
    }

    // Free text has no stable class and field ID and needs phrase overrides.
    private static String calloutId(CalloutEvent ev) {
        if (ev instanceof CustomTriggers.Callout custom) {
            return custom.id;
        }
        return idForField(calloutField(ev));
    }

    private static Field calloutField(CalloutEvent ev) {
        if (ev instanceof CustomTriggers.Callout) return null;
        final CalloutTraceInfo trace = ev.getTrace();
        if (trace instanceof ModifiableCalloutTraceInfo mti) {
            return mti.getCalloutField();
        }
        return null;
    }

    // Failed setting writes must not block remaining in-memory changes.
    private static void handleCommand(JsonNode n) {
        try {
            final String cmd = n.path("nyaa_cmd").asText("");
            if ("cancel_speech".equals(cmd)) {
                var ids = new HashSet<String>();
                for (JsonNode id : n.path("ids")) {
                    if (id.isTextual()) {
                        ids.add(id.asText());
                    }
                }
                synchronized (PENDING_SPEECH) {
                    if (n.path("all").asBoolean(false)) {
                        PENDING_SPEECH.clear();
                    } else {
                        PENDING_SPEECH.values().removeIf(pending -> ids.contains(pending.id()));
                    }
                    println("{\"t\":\"speech_canceled\",\"token\":" + n.path("token").asLong(0) + "}");
                }
                return;
            }
            if ("custom_triggers".equals(cmd)) {
                CustomTriggers.Configure config = new CustomTriggers.Configure(n.path("triggers"));
                MASTER.pushEventAndWait(config);
                println(MAPPER.writeValueAsString(Map.of("t", "custom_triggers",
                        "ok", config.error == null, "message", config.error == null ? "" : config.error)));
                return;
            }
            if ("pause_feed".equals(cmd)) {
                applyAutomark(false);
                RECOVERY.begin(RECOVERY.clock.now().toString());
                cancelPendingSpeech();
                return;
            }
            if ("recover_log".equals(cmd)) {
                applyAutomark(false);
                recoveryStatus = "failed";
                recoveryReason = "History restoration did not finish";
                RECOVERY.begin(n.path("time").asText());
                cancelPendingSpeech();
                JsonNode history = n.path("history");
                List<String> snapshots = new ArrayList<>();
                for (JsonNode snapshot : n.path("state")) {
                    snapshots.add(MAPPER.writeValueAsString(snapshot));
                }
                var restored = RECOVERY.restore(Path.of(history.path("folder").asText()),
                        history.path("anchor").asText(), history.path("zone").asLong(),
                        history.path("player").asLong(), snapshots, Instant.parse(n.path("time").asText()));
                recoveryReason = restored.reason();
                recoveryStatus = restored.lines().isEmpty() ? "unavailable"
                        : restored.skipped() > 0 ? "degraded" : "complete";
                diag("recovery: restored " + restored.lines().size() + " historical events"
                        + ", skipped " + restored.skipped()
                        + (restored.reason().isEmpty() ? "" : ", " + restored.reason()));
                return;
            }
            if ("recover_begin".equals(cmd)) {
                applyAutomark(false);
                RECOVERY.begin(n.path("time").asText());
                cancelPendingSpeech();
                recoveryStatus = "state_only";
                recoveryReason = "";
                return;
            }
            if ("recover_checkpoint".equals(cmd)) {
                recoveryProgress("recovery_checkpoint", n);
                return;
            }
            if ("recover_end".equals(cmd)) {
                RECOVERY.advance(Instant.now());
                RECOVERY.end();
                if (automarkCommand != null) {
                    handleAutomark(automarkCommand);
                }
                else {
                    applyAutomark(requestedAutomark);
                }
                recoveryProgress("recovered", n);
                return;
            }
            if ("set_automark".equals(cmd)) {
                JsonNode merged = mergeAutomark(n);
                AutomarkControl control = prepareAutomark(merged);
                automarkCommand = merged;
                requestedAutomark = control.enable();
                if (!RECOVERY.clock.replaying()) {
                    handleAutomark(control);
                    if (n.has("settings")) emitAutomarkInventory(null);
                }
                return;
            }
            final String id = n.path("id").asText(null);
            final ModifiedCalloutHandle h = (id == null) ? null : CALLOUTS.get(id);
            if (h == null) {
                diag("command: unknown callout id " + id);
                return;
            }
            String previousTts = h.getEffectiveTts();
            if ("set_callout".equals(cmd)) {
                if (n.hasNonNull("tts")) {
                    trySet(() -> h.getTtsSetting().set(n.get("tts").asText("")));
                    trySet(() -> h.getEnableTts().set(true));
                }
                if (n.hasNonNull("text")) {
                    trySet(() -> h.getTextSetting().set(n.get("text").asText("")));
                    trySet(() -> h.getEnableText().set(true));
                }
                if (n.hasNonNull("enable")) {
                    trySet(() -> h.getEnable().set(n.get("enable").asBoolean(true)));
                }
                diag("set_callout applied: " + id);
            } else if ("reset_callout".equals(cmd)) {
                trySet(() -> h.getTtsSetting().delete());
                trySet(() -> h.getTextSetting().delete());
                diag("reset_callout applied: " + id);
            }
            if (!Objects.equals(previousTts, h.getEffectiveTts())) {
                synchronized (PENDING_SPEECH) {
                    PENDING_SPEECH.values().removeIf(pending -> id.equals(pending.id()));
                }
            }
        } catch (Throwable t) {
            if (n.path("nyaa_cmd").asText("").startsWith("recover_")) {
                recoveryStatus = "failed";
                recoveryReason = t.toString();
            }
            diag("command error: " + t);
            diagnosticError("command", t);
            if ("set_automark".equals(n.path("nyaa_cmd").asText())) {
                emitAutomarkInventory(t.getMessage());
            }
        }
    }

    private static void recoveryProgress(String kind, JsonNode command) {
        int skipped = RECOVERY.skipped();
        if (skipped > 0 && ("complete".equals(recoveryStatus) || "state_only".equals(recoveryStatus))) {
            recoveryStatus = "degraded";
        }
        println(MAPPER.writeValueAsString(Map.of("t", kind, "checkpoint", command.path("checkpoint").asInt(0),
                "status", recoveryStatus, "reason", recoveryReason, "skipped", skipped)));
    }

    private static void requestCombatants(java.util.Collection<Long> ids) {
        if (!RECOVERY.clock.replaying()) {
            println(MAPPER.writeValueAsString(Map.of("t", "combatants_request", "ids", ids)));
        }
    }

    private static void trySet(Runnable r) {
        try {
            r.run();
        } catch (Throwable t) {
            diag("setting write skipped: " + t);   // The live value is already updated.
        }
    }

    private record AutomarkControl(URI uri, boolean enable, boolean nativeUmad,
                                   NativeAutomarkers.Plan settings) {}

    private static JsonNode mergeAutomark(JsonNode command) {
        var merged = MAPPER.createObjectNode();
        if (automarkCommand != null) automarkCommand.properties().forEach(e -> merged.set(e.getKey(), e.getValue()));
        command.properties().forEach(e -> merged.set(e.getKey(), e.getValue()));
        if (command.path("settings").isObject() && automarkCommand != null
                && automarkCommand.path("settings").isObject()) {
            var settings = MAPPER.createObjectNode();
            automarkCommand.path("settings").properties().forEach(e -> settings.set(e.getKey(), e.getValue()));
            command.path("settings").properties().forEach(e -> settings.set(e.getKey(), e.getValue()));
            merged.set("settings", settings);
        }
        return merged;
    }

    private static AutomarkControl prepareAutomark(JsonNode n) {
        for (String key : List.of("enable", "native_umad")) {
            if (n.has(key) && !n.get(key).isBoolean()) {
                throw new IllegalArgumentException(key + " must be a boolean");
            }
        }
        URI uri = null;
        if (n.has("uri")) {
            if (!n.get("uri").isTextual()) throw new IllegalArgumentException("uri must be a URL");
            uri = URI.create(n.get("uri").asText().trim());
            if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                    || uri.getHost() == null || uri.getPort() < -1 || uri.getPort() == 0 || uri.getPort() > 65535) {
                throw new IllegalArgumentException("uri must be an HTTP or HTTPS URL with a valid host and port");
            }
        }
        if (NATIVE_AUTOMARKERS == null && n.has("settings")) {
            throw new IllegalStateException("Native automarker settings are unavailable");
        }
        return new AutomarkControl(uri, n.path("enable").asBoolean(requestedAutomark),
                n.path("native_umad").asBoolean(nativeUmadEnabled),
                NATIVE_AUTOMARKERS == null ? null : NATIVE_AUTOMARKERS.plan(n.path("settings")));
    }

    private static void handleAutomark(JsonNode n) {
        handleAutomark(prepareAutomark(n));
        if (n.has("settings")) emitAutomarkInventory(null);
    }

    private static void handleAutomark(AutomarkControl control) {
        URI uri = control.uri();
        boolean uriChanged = uri != null && TELESTO != null && !uri.equals(TELESTO.getUriSetting().get());
        boolean enable = control.enable();
        boolean nativeUmad = control.nativeUmad();
        boolean settingsChanged = control.settings() != null && control.settings().changed();
        boolean enabled = AM_SELECTOR != null && "telesto-am".equals(AM_SELECTOR.getEffectiveOption().descriptor().id());
        boolean effective = enable && automarkAvailable();
        if (uriChanged || effective != enabled || nativeUmad != nativeUmadEnabled || settingsChanged) {
            RECOVERY.cancelPendingOutput();
        }
        if (uriChanged || settingsChanged) {
            selectAutomarkService("none", settingsChanged);
        }
        if (control.settings() != null) control.settings().apply(TriggeventCore::trySet);
        if (uriChanged) {
            URI newUri = uri;
            trySet(() -> TELESTO.getUriSetting().set(newUri));
        }
        if (nativeUmad != nativeUmadEnabled) {
            nativeUmadEnabled = nativeUmad;
            if (DMU_TRIGGERS != null) {
                DMU_TRIGGERS.setKefkaAutoMarksEnabled(nativeUmad);
            }
        }
        applyAutomark(enable);
        diag("set_automark applied");
    }

    private static void onAutomarkFailure(BaseTelestoResponse event) {
        if (event.isCurrent() && AM_SELECTOR != null
                && "telesto-am".equals(AM_SELECTOR.getEffectiveOption().descriptor().id())) {
            selectAutomarkService("none");
            selectAutomarkService("telesto-am");
        }
    }

    // Refresh party order on enable. Slots remain unknown until the reply arrives.
    private static void applyAutomark(boolean enable) {
        boolean available = automarkAvailable();
        boolean effective = enable && available;
        boolean changed = AM_SELECTOR != null && !Objects.equals(AM_SELECTOR.getEffectiveOption().descriptor().id(),
                effective ? "telesto-am" : "none");
        if (changed) RECOVERY.cancelPendingOutput();
        if (TELESTO != null && TELESTO.getEnablePartyList().get() != effective) {
            trySet(() -> TELESTO.getEnablePartyList().set(effective));
        }
        selectAutomarkService(effective ? "telesto-am" : "none");
        diagnostics.automarkConfig(enable, available, nativeUmadEnabled);
        if (effective && changed) {
            trySet(TELESTO::refreshPartyIfEnabled);
        }
    }

    // Select by ID so upstream priorities cannot choose another marker handler.
    private static void selectAutomarkService(String id) {
        selectAutomarkService(id, false);
    }

    private static boolean automarkAvailable() {
        return TELESTO != null && AM_SELECTOR != null && AM_SELECTOR.getOptions().stream()
                .anyMatch(handle -> "telesto-am".equals(handle.descriptor().id()));
    }

    private static void selectAutomarkService(String id, boolean notify) {
        if (AM_SELECTOR == null) {
            return;
        }
        if (!notify && id.equals(AM_SELECTOR.getEffectiveOption().descriptor().id())) {
            return;
        }
        trySet(() -> AM_SELECTOR.getOptions().stream()
                .filter(hh -> id.equals(hh.descriptor().id()))
                .findFirst()
                .ifPresent(ServiceHandle::setEnabled));
    }

    private static String idForField(Field f) {
        if (f == null) {
            return null;
        }
        final String cn = f.getDeclaringClass().getCanonicalName();
        return cn == null ? null : cn + '.' + f.getName();
    }

    private static void emitInventory(MutablePicoContainer pico) {
        final ModifiedCalloutRepository repo = pico.getComponent(ModifiedCalloutRepository.class);
        if (repo == null) {
            diag("no ModifiedCalloutRepository - inventory skipped");
            return;
        }
        List<Map<String, String>> entries = new ArrayList<>();
        final List<CalloutGroup> groups = repo.getAllCallouts();
        diagnostics.registerSequences(repo);
        for (CalloutGroup g : groups) {
            final String fight = g.getDuty() == null ? "None" : g.getDuty().name();
            final String groupName = g.getName();
            for (ModifiedCalloutHandle h : g.getCallouts()) {
                final String id = idForField(h.getField());
                if (id == null) {
                    continue;
                }
                CALLOUTS.put(id, h);
                String text = h.getOriginal().getOriginalVisualText();
                if (text == null || text.isEmpty()) {
                    text = h.getOriginal().getOriginalTts();
                }
                Map<String, String> entry = new LinkedHashMap<>();
                entry.put("id", id);
                entry.put("name", h.getDescription());
                entry.put("fight", fight);
                entry.put("group", groupName);
                entry.put("text", text);
                entry.put("tts", Objects.requireNonNullElse(h.getOriginal().getOriginalTts(), ""));
                entry.values().removeIf(Objects::isNull);
                entries.add(entry);
            }
        }
        println(MAPPER.writeValueAsString(Map.of("t", "inventory", "triggers", entries)));
        emitAutomarkInventory(null);
        diag("inventory emitted: " + groups.size() + " groups");
    }

    private static void emitAutomarkInventory(String error) {
        if (NATIVE_AUTOMARKERS == null) return;
        Map<String, Object> inventory = new LinkedHashMap<>(NATIVE_AUTOMARKERS.inventory());
        if (error != null) inventory.put("error", error);
        println(MAPPER.writeValueAsString(inventory));
    }

    private static String severity(Color c) {
        if (c == null) {
            return "info";
        }
        if (c.getRed() >= 180 && c.getGreen() < 120 && c.getBlue() < 120) {
            return "alarm";
        }
        return "alert";
    }

    private static void emitStatus(boolean active, String msg) {
        println(MAPPER.writeValueAsString(Map.of("t", "status", "active", active, "message", msg)));
    }

    private static void println(String s) {
        synchronized (OUT) {
            OUT.println(s);
            OUT.flush();
        }
    }

    private static void diag(String m) {
        System.err.println("[triggevent-core] " + m);
    }

    private static void diagnosticError(String site, Throwable error) {
        EngineDiagnostics current = diagnostics;
        if (current == null) {
            current = new EngineDiagnostics(null, record -> println(MAPPER.writeValueAsString(record)));
        }
        current.error(site, error);
    }
}
