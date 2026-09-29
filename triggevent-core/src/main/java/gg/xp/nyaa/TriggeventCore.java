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

/**
 * Run Triggevent triggers from IINACT messages on stdin and emit resolved callouts as
 * JSON lines. Uses the engine's built in triggers, Groovy scripts and EasyTriggers
 * without opening another ACT connection.
 */
public final class TriggeventCore {

    // Use UTF-8 explicitly because the bundled JDK 17 can otherwise use a Windows code
    // page.
    private static final PrintStream OUT = new PrintStream(System.out, true, StandardCharsets.UTF_8);

    // Set NYAA_TV_DIAG=1 to print event counts on exit.
    private static final boolean DIAG_ON = System.getenv("NYAA_TV_DIAG") != null;
    private static final Map<String, AtomicLong> DIAG = new LinkedHashMap<>();

    // Index modifiable callouts by stable ID for live edits and suppression.
    private static final Map<String, ModifiedCalloutHandle> CALLOUTS = new HashMap<>();
    private static final ObjectMapper MAPPER = new ObjectMapper();

    // Sequence emitted callouts so the parent can detect delivery gaps.
    private static final AtomicLong CALLOUT_SEQ = new AtomicLong();
    private static final int MAX_PENDING_SPEECH = 1024;
    private static final Map<CalloutTrackingKey, PendingSpeech> PENDING_SPEECH = new LinkedHashMap<>();
    private static long speechEpoch;
    private record PendingSpeech(long epoch, String id,
                                 ModifiedCalloutHandle handle, String template) {}

    // Keep the engine Telesto integration available for explicit control commands. It
    // remains absent when telesto-core is not on the classpath.
    private static volatile TelestoMain TELESTO;
    private static volatile AutoMarkServiceSelector AM_SELECTOR;
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
        // Do not force AWT headless mode. Engine components construct Swing windows
        // during startup. The bridge supplies Xvfb when available, otherwise the
        // session display.

        try {
            final MutablePicoContainer pico = bootEngine();
            final EventMaster master = pico.getComponent(EventMaster.class);

            emitStatus(true, "Triggevent Engine ready");
            diag("ready; reading WS messages on stdin; recovery=1; history=1; catchup=1; custom=1; speech_cancel=1; speech_cancel_all=1; diagnostics=1");

            // InitEvent handlers populate the callout registry synchronously. Drain any
            // queued followup work before publishing inventory.
            try {
                pico.getComponent(BasicEventQueue.class).waitDrain();
                emitInventory(pico);
            } catch (Throwable t) {
                diag("inventory error: " + t);
                diagnosticError("inventory", t);
            }

            // Pass raw IINACT messages through the engine's normal event handlers.
            final BufferedReader in =
                    new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                try {
                    // Python rejects reserved command keys from the WebSocket feed.
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
            // Drain queued events on EOF and allow brief delayed callouts to finish
            // before exit.
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
            // Log boot failures and return an error before finally exits the JVM.
            t.printStackTrace();
            diagnosticError("boot", t);
            OUT.flush();
            System.exit(1);
        } finally {
            // Exit explicitly because engine event threads would otherwise keep the JVM
            // alive after stdin closes.
            OUT.flush();
            System.exit(0);
        }
    }

    /**
     * Initialize without opening a live ACT connection. Reflectively use the private
     * requiredComponents bootstrap, then load user settings without writing them,
     * attach the callout sink and run startup events.
     */
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

        // Keep replay parsers enabled so log lines can reconstruct player, zone and
        // party state.
        pico.getComponent(AutoHandlerConfig.class).setNotLive(true);

        // Mark the source as live for Telesto dispatch and party polling. This does not
        // start an ACT WebSocket and is separate from enabling replay parsers.
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

        // Select the none marker service by default. The higher priority keyboard
        // handler would otherwise send keys to the focused window. Telesto requires an
        // explicit control command.
        try {
            TELESTO = pico.getComponent(TelestoMain.class);
            if (TELESTO != null) {
                TELESTO.setOutgoingGate(message -> RECOVERY.outputAllowed()
                        && !message.getEffectiveHappenedAt().isBefore(Instant.now().minusSeconds(3)));
                TELESTO.setDeliveryPermit(RECOVERY::outputPermit);
            }
            AM_SELECTOR = pico.getComponent(AutoMarkServiceSelector.class);
            final String envUri = System.getenv("NYAA_TELESTO_URI");
            if (envUri != null && !envUri.isBlank() && TELESTO != null) {
                trySet(() -> TELESTO.getUriSetting().set(URI.create(envUri.trim())));
            }
            // Disable Telesto polling until requested. NYAA_AUTOMARK=1 enables it for
            // standalone debugging.
            requestedAutomark = "1".equals(System.getenv("NYAA_AUTOMARK"));
            applyAutomark(requestedAutomark);
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

    /**
     * Receive resolved callouts from built in triggers, EasyTriggers and Groovy.
     */
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
            final StringBuilder sb = new StringBuilder(128);
            sb.append("{\"t\":\"callout\"");
            long seq = CALLOUT_SEQ.incrementAndGet();
            sb.append(",\"seq\":").append(seq);
            Field sourceField = calloutField(ev);
            field(sb, "id", ev instanceof CustomTriggers.Callout custom ? custom.id : idForField(sourceField));
            field(sb, "tts", tts);
            field(sb, "text", text);
            if (ttsOnly) {
                sb.append(",\"tts_only\":true");
            }
            sb.append(",\"at\":").append(at.toEpochMilli());
            sb.append(",\"severity\":\"").append(severity(ev.getColorOverride())).append('"');
            if (!ttsOnly) {
                field(sb, "sound", ev.getSound());
            }
            boolean expired = ev.isExpired();
            sb.append(",\"expired\":").append(expired);
            sb.append('}');
            diagnostics.callout(sourceField, seq, ttsOnly, text != null && !text.isBlank(),
                    tts != null && !tts.isBlank(), expired);
            println(sb.toString());
            // Report PrintStream write failures once per failure streak.
            if (OUT.checkError()) {
                diag("stdout write failed while emitting a callout");
            }
        } catch (Throwable t) {
            diag("emit error: " + t);
            diagnosticError("callout", t);
        }
    }

    /**
     * Emit engine Telesto connection status after requests. Requires the live source
     * setting that enables Telesto dispatch.
     */
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

    /**
     * Return the stable declaring class and field ID for a modifiable callout, or null
     * for free text that needs phrase overrides.
     */
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

    /**
     * Apply callout edits or reset defaults from control messages. Guard each setting
     * write so read only persistence cannot block the remaining in memory changes.
     */
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
            // Dispatch automarker commands before requiring a callout ID.
            if ("set_automark".equals(cmd)) {
                automarkCommand = n;
                requestedAutomark = n.path("enable").asBoolean(false);
                if (!RECOVERY.clock.replaying()) {
                    handleAutomark(n);
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

    /**
     * Apply the requested Telesto URL and enabled state from a control message. Use
     * telesto-am when enabled and none otherwise.
     */
    private static void handleAutomark(JsonNode n) {
        RECOVERY.cancelPendingOutput();
        if (n.hasNonNull("uri") && TELESTO != null) {
            trySet(() -> TELESTO.getUriSetting().set(URI.create(n.get("uri").asText().trim())));
        }
        if (n.has("enable")) {
            applyAutomark(n.get("enable").asBoolean(true));
        }
        diag("set_automark applied");
    }

    /**
     * Change marker service and party polling together. Refresh party order immediately
     * on enable, though slots remain unresolved until the reply arrives.
     */
    private static void applyAutomark(boolean enable) {
        if (!enable) {
            RECOVERY.cancelPendingOutput();
        }
        if (TELESTO != null) {
            trySet(() -> TELESTO.getEnablePartyList().set(enable));
        }
        selectAutomarkService(enable ? "telesto-am" : "none");
        if (enable && TELESTO != null) {
            trySet(TELESTO::refreshPartyIfEnabled);
        }
    }

    /**
     * Select the registered marker service by ID so upstream priority changes cannot
     * select another handler.
     */
    private static void selectAutomarkService(String id) {
        if (AM_SELECTOR == null) {
            return;
        }
        trySet(() -> AM_SELECTOR.getOptions().stream()
                .filter(hh -> id.equals(hh.descriptor().id()))
                .findFirst()
                .ifPresent(ServiceHandle::setEnabled));
    }

    /**
     * Derive a stable inventory and callout ID from the declaring class and field.
     */
    private static String idForField(Field f) {
        if (f == null) {
            return null;
        }
        final String cn = f.getDeclaringClass().getCanonicalName();
        return cn == null ? null : cn + '.' + f.getName();
    }

    /**
     * Publish the modifiable callout inventory at startup. Include stable ID,
     * description, duty, group and default text for each row.
     */
    private static void emitInventory(MutablePicoContainer pico) {
        final ModifiedCalloutRepository repo = pico.getComponent(ModifiedCalloutRepository.class);
        if (repo == null) {
            diag("no ModifiedCalloutRepository - inventory skipped");
            return;
        }
        final StringBuilder sb = new StringBuilder(4096);
        sb.append("{\"t\":\"inventory\",\"triggers\":[");
        boolean first = true;
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
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append("{\"id\":\"").append(esc(id)).append('"');
                field(sb, "name", h.getDescription());
                field(sb, "fight", fight);
                field(sb, "group", groupName);
                field(sb, "text", text);
                field(sb, "tts", Objects.requireNonNullElse(h.getOriginal().getOriginalTts(), ""));
                sb.append('}');
            }
        }
        sb.append("]}");
        println(sb.toString());
        diag("inventory emitted: " + groups.size() + " groups");
    }

    /**
     * Infer severity from the color override. Strong red is alarm, another override is
     * alert, and no override is info.
     */
    private static String severity(Color c) {
        if (c == null) {
            return "info";
        }
        if (c.getRed() >= 180 && c.getGreen() < 120 && c.getBlue() < 120) {
            return "alarm";
        }
        return "alert";
    }

    // Write JSON without depending on the engine's Jackson version.
    private static void field(StringBuilder sb, String key, String val) {
        if (val == null) {
            return;
        }
        sb.append(",\"").append(key).append("\":\"").append(esc(val)).append('"');
    }

    private static String esc(String s) {
        final StringBuilder b = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            final char ch = s.charAt(i);
            switch (ch) {
                case '"':  b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n");  break;
                case '\r': b.append("\\r");  break;
                case '\t': b.append("\\t");  break;
                default:
                    if (ch < 0x20) {
                        b.append(String.format("\\u%04x", (int) ch));
                    } else {
                        b.append(ch);
                    }
            }
        }
        return b.toString();
    }

    private static void emitStatus(boolean active, String msg) {
        println("{\"t\":\"status\",\"active\":" + active + ",\"message\":\"" + esc(msg) + "\"}");
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
