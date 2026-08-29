package gg.xp.nyaa;

import gg.xp.reevent.events.BasicEventQueue;
import gg.xp.reevent.events.EventContext;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.reevent.events.InitEvent;
import gg.xp.reevent.scan.AutoHandlerConfig;
import gg.xp.xivsupport.callouts.CalloutGroup;
import gg.xp.xivsupport.callouts.ModifiedCalloutHandle;
import gg.xp.xivsupport.callouts.ModifiedCalloutRepository;
import gg.xp.xivsupport.callouts.RawModifiedCallout;
import gg.xp.xivsupport.events.ACTLogLineEvent;
import gg.xp.xivsupport.events.actlines.events.AbilityCastStart;
import gg.xp.xivsupport.events.actlines.events.AbilityUsedEvent;
import gg.xp.xivsupport.events.actlines.events.BuffApplied;
import gg.xp.xivsupport.events.ws.ActWsRawMsg;
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
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Headless Triggevent Engine sidecar for NyaaTriggers.
 *
 * <p>Boots Triggevent's real engine (xpdota/event-trigger) with NO GUI and NO
 * live ACT WebSocket, reads raw IINACT/OverlayPlugin WS messages teed from
 * NyaaTriggers on stdin (one JSON object per line), runs ALL Triggevent triggers
 * (built-in Java + user Groovy + EasyTriggers), and writes every resolved callout
 * back to stdout as JSON lines:
 *
 * <pre>{"t":"callout","tts":"...","text":"...","severity":"info|alert|alarm","sound":"...","expired":false}</pre>
 *
 * <p>This is the data-capture counterpart to {@code ../triggevent_bridge.py}.
 */
public final class TriggeventCore {

    // Pin stdout to UTF-8, matching the stdin pin in main (and the .NET host, which
    // pins both directions): release builds bundle Temurin 17 and UTF-8-by-default
    // only arrived in JDK 18, so on Windows System.out would encode with the system
    // code page and mangle non-ASCII callout/inventory text.
    private static final PrintStream OUT = new PrintStream(System.out, true, StandardCharsets.UTF_8);

    // Set NYAA_TV_DIAG=1 to count key events through the pipeline (printed on exit).
    // Lets you see whether log lines parse, triggers fire, and callouts get emitted.
    private static final boolean DIAG_ON = System.getenv("NYAA_TV_DIAG") != null;
    private static final Map<String, AtomicLong> DIAG = new LinkedHashMap<>();

    // Registry of every modifiable callout by its stable id (built at boot in
    // emitInventory). Lets NyaaTriggers edit a trigger's spoken/visual text or
    // toggle it via set_callout/reset_callout commands on stdin.
    private static final Map<String, ModifiedCalloutHandle> CALLOUTS = new HashMap<>();
    private static final ObjectMapper MAPPER = new ObjectMapper();

    // Monotonic sequence stamped on every emitted callout JSON line. NyaaTriggers
    // gaps-check it to catch callouts lost between the engine and the app.
    private static final AtomicLong CALLOUT_SEQ = new AtomicLong();

    // Triggevent's own Telesto integration, captured at boot so NyaaTriggers can drive
    // automarks through the user's Telesto Dalamud plugin (HTTP server, default
    // http://localhost:45678/). TelestoMain pulls the game party list (GetPartyMembers)
    // to correct slot order and POSTs "/mk attack <slot>" style commands; the engine
    // owns slot resolution, the command-delay throttle, language and clear logic. Null
    // on an engine build without telesto-core on the classpath (feature stays inert).
    private static volatile TelestoMain TELESTO;
    private static volatile AutoMarkServiceSelector AM_SELECTOR;

    private static AtomicLong diagCount(String key) {
        synchronized (DIAG) {
            return DIAG.computeIfAbsent(key, k -> new AtomicLong());
        }
    }

    private TriggeventCore() {
    }

    public static void main(String[] args) throws Exception {
        // NOTE: do NOT force java.awt.headless=true. Some auto-scanned engine
        // components (e.g. PartyOverlay) build a Swing JFrame in their constructor;
        // under forced-headless that throws HeadlessException and aborts boot. The
        // sidecar instead runs against a display - ideally a throwaway Xvfb (launched
        // by the Python bridge) so Triggevent's own overlays never touch the user's
        // screen; we only harvest its CalloutEvents. Falls back to the session display.

        try {
            final MutablePicoContainer pico = bootEngine();
            final EventMaster master = pico.getComponent(EventMaster.class);

            emitStatus(true, "Triggevent Engine ready");
            diag("ready; reading WS messages on stdin");

            // InitEvent is dispatched synchronously in bootEngine (its @HandleEvents
            // handlers run inline on the calling thread), so ModifiedCalloutRepository is
            // already populated by the time we get here. The waitDrain() is just a
            // defensive flush of anything the Init handlers enqueued; then publish a
            // one-shot inventory of every modifiable (read-only) callout so NyaaTriggers
            // can show grouped read-only rows and suppress specific ids.
            try {
                pico.getComponent(BasicEventQueue.class).waitDrain();
                emitInventory(pico);
            } catch (Throwable t) {
                diag("inventory error: " + t);
            }

            // Feed loop: each stdin line is a raw OverlayPlugin/IINACT WS message.
            // ActWsRawMsg + the engine's ActWsHandlers dispatch it into domain events
            // (LogLine -> ACTLogLineEvent, CombatData, ChangePrimaryPlayer, ChangeZone,
            // PartyChanged, ...) exactly as in live mode.
            final BufferedReader in =
                    new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                // NyaaTriggers control commands are multiplexed onto the same stdin as
                // the teed WS feed; they carry a "nyaa_cmd" key (WS messages never do).
                if (line.contains("\"nyaa_cmd\"")) {
                    handleCommand(line);
                    continue;
                }
                try {
                    master.pushEvent(new ActWsRawMsg(line));
                } catch (Throwable t) {            // never let one bad line kill the feed
                    diag("feed error: " + t);
                }
            }
            // stdin closed (live shutdown, or end of an offline log replay): drain the
            // event queue so in-flight callouts still get emitted before we exit. The
            // brief grace loop lets short wall-clock-delayed callouts (which fire on a
            // separate timer thread) land too - matters only for offline log replay,
            // where the whole log is fed in milliseconds.
            try {
                final BasicEventQueue q = pico.getComponent(BasicEventQueue.class);
                q.waitDrain();
                for (int i = 0; i < 4; i++) {
                    Thread.sleep(120);
                    q.waitDrain();
                }
            } catch (Throwable ignored) {
                // queue component absent / interrupted - nothing to drain
            }
            if (DIAG_ON) {
                final StringBuilder sb = new StringBuilder("event counts:");
                synchronized (DIAG) {
                    DIAG.forEach((k, v) -> sb.append(' ').append(k).append('=').append(v.get()));
                }
                diag(sb.toString());
            }
            emitStatus(false, "stdin closed");
        } catch (Throwable t) {
            // A boot failure must not look like a clean stop. System.exit in the
            // finally below runs before the default uncaught handler, so without
            // this catch the stack trace would never print and the JVM would
            // exit 0, indistinguishable from a normal stdin EOF shutdown.
            t.printStackTrace();
            OUT.flush();
            System.exit(1);
        } finally {
            // The engine's EventPump threads are NON-DAEMON (EventMaster builds its
            // factory with daemon(false)), so main returning would NOT end the JVM:
            // on parent death the sidecar gets only stdin EOF and would orphan a JVM
            // (plus its Xvfb) per hard crash - the orphan class Program.cs's
            // stdin-reader finally guards against on the .NET side. try/finally (not
            // a trailing statement) so an exception out of bootEngine after the pump
            // started also exits.
            OUT.flush();
            System.exit(0);
        }
    }

    /**
     * Stand up the engine headlessly with the user's real Triggevent data.
     *
     * <p>{@code XivMain.masterInit()} force-starts a live ACT WebSocket (which would
     * duplicate our teed feed), and {@code requiredComponents()} (the clean, no-live-WS
     * bootstrap) is private. So we invoke it reflectively, then wire the rest with
     * public APIs: real user persistence (loads EasyTriggers + settings, read-only so
     * we never clobber the user's file), log-replay mode, our callout sink, InitEvent
     * (runs user Groovy startup scripts), and start.
     */
    private static MutablePicoContainer bootEngine() throws Exception {
        final Method requiredComponents = XivMain.class.getDeclaredMethod("requiredComponents");
        requiredComponents.setAccessible(true);
        final MutablePicoContainer pico =
                (MutablePicoContainer) requiredComponents.invoke(null);

        // Real user data folder (~/.triggevent on Linux), read-only.
        pico.addComponent(UserDirPropsPersistenceProvider.inUserDataFolder("triggevent", true));

        // Log-replay mode: keep the import-only parsers that reconstruct player/zone/
        // party state from log lines enabled (they self-disable in live OP-feed mode).
        pico.getComponent(AutoHandlerConfig.class).setNotLive(true);

        // Telesto automark egress gate. TelestoMain.enabled() (and its GetPartyMembers
        // party-order poll, which - together with pull-party-list ON and a completed
        // round-trip, both handled in applyAutomark - is needed for "/mk attack <slot>"
        // to hit the RIGHT player) is gated on a WEBSOCKET_LIVE log source. We feed a
        // live OP/IINACT tee, so this
        // is accurate; it does NOT open any ACT WebSocket (that is ActWsLogSource.start(),
        // which the sidecar never calls) and is orthogonal to setNotLive above. Without
        // it the entire Telesto path is inert. Side effect: RawEventStorage retains raw
        // events (normal live behaviour) - harmless for our short-lived feed.
        pico.getComponent(PrimaryLogSource.class).setLogSource(KnownLogSource.WEBSOCKET_LIVE);

        final EventDistributor dist = pico.getComponent(EventDistributor.class);
        dist.registerHandler(CalloutEvent.class, TriggeventCore::onCallout);
        dist.registerHandler(TelestoStatusUpdatedEvent.class, TriggeventCore::onTelestoStatus);

        if (DIAG_ON) {
            dist.registerHandler(ACTLogLineEvent.class, (c, e) -> diagCount("ACTLogLineEvent").incrementAndGet());
            dist.registerHandler(AbilityCastStart.class, (c, e) -> diagCount("AbilityCastStart").incrementAndGet());
            dist.registerHandler(AbilityUsedEvent.class, (c, e) -> diagCount("AbilityUsedEvent").incrementAndGet());
            dist.registerHandler(BuffApplied.class, (c, e) -> diagCount("BuffApplied").incrementAndGet());
            dist.registerHandler(RawModifiedCallout.class, (c, e) -> diagCount("RawModifiedCallout").incrementAndGet());
            dist.registerHandler(CalloutEvent.class, (c, e) -> diagCount("CalloutEvent").incrementAndGet());
            dist.registerHandler(TtsRequest.class, (c, e) -> diagCount("TtsRequest").incrementAndGet());
        }

        dist.acceptEvent(new InitEvent());                 // runs startup Groovy, etc.
        pico.getComponent(EventMaster.class).start();

        // Capture Triggevent's Telesto components (instantiated + bus-wired by the scan
        // during start()). Then force a SAFE automark default: select "none". This is
        // critical because the built-in keyboard-macro handler registers at priority 15
        // (higher than telesto-am's 12) and is therefore the DEFAULT selected service -
        // it injects AWT-Robot F-key presses into whatever window has focus, which is
        // wrong AND harmful on the headless/Proton box. NyaaTriggers opts in to Telesto
        // explicitly via a set_automark command. "none" is always a registered default
        // option, so this is safe even if telesto-core is absent.
        try {
            TELESTO = pico.getComponent(TelestoMain.class);
            AM_SELECTOR = pico.getComponent(AutoMarkServiceSelector.class);
            final String envUri = System.getenv("NYAA_TELESTO_URI");
            if (envUri != null && !envUri.isBlank() && TELESTO != null) {
                trySet(() -> TELESTO.getUriSetting().set(URI.create(envUri.trim())));
            }
            // Default OFF: select "none" AND disable Telesto party polling so the engine
            // makes ZERO contact with the Telesto plugin until NyaaTriggers opts in (the
            // {"nyaa_cmd":"set_automark"} line). NYAA_AUTOMARK=1 opts in at boot for
            // standalone debug runs.
            applyAutomark("1".equals(System.getenv("NYAA_AUTOMARK")));
            // One-shot boot diagnostic: confirms telesto-core scanned (telesto-am
            // registered) and which actuator is selected. Invaluable for field debugging
            // ("is the automark backend even wired?").
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
            diag("telesto wiring skipped: " + t);   // engine without telesto-core -> inert
        }
        return pico;
    }

    /** Central harvest point: every resolved callout (built-in / EasyTrigger / Groovy). */
    private static void onCallout(EventContext ctx, CalloutEvent ev) {
        try {
            final StringBuilder sb = new StringBuilder(128);
            sb.append("{\"t\":\"callout\"");
            // Monotonic per-sidecar-generation sequence so the app can tell a
            // callout lost on the wire (seq gap) apart from one never emitted.
            sb.append(",\"seq\":").append(CALLOUT_SEQ.incrementAndGet());
            field(sb, "id", calloutId(ev));            // stable per-trigger id (may be null)
            field(sb, "tts", ev.getCallText());        // spoken text
            field(sb, "text", ev.getVisualText());     // on-screen text
            sb.append(",\"severity\":\"").append(severity(ev.getColorOverride())).append('"');
            field(sb, "sound", ev.getSound());
            sb.append(",\"expired\":").append(ev.isExpired());
            sb.append('}');
            println(sb.toString());
            // PrintStream swallows write failures; surface them once per streak.
            if (OUT.checkError()) {
                diag("stdout write failed while emitting a callout");
            }
        } catch (Throwable t) {
            diag("emit error: " + t);
        }
    }

    /**
     * Tee Telesto connection status so NyaaTriggers can show a live indicator.
     *   {"t":"telesto","status":"good|bad|unknown"}
     * GOOD after a successful POST to the Telesto plugin, BAD on a connection error.
     * Only fires once the WEBSOCKET_LIVE log source (set in bootEngine) has enabled
     * TelestoMain's egress; otherwise TelestoMain never updates its status.
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
     * Stable per-trigger id for a harvested callout, or null. The only production
     * trace type is ModifiableCalloutTraceInfo (set in CalloutProcessor); its field
     * (when present) is the static ModifiableCallout field that produced the call.
     * The id is stable across runs/updates (it is derived from the declaring class +
     * field name). Null for callouts with no modifiable-field origin (free Groovy text),
     * which NyaaTriggers can then only suppress via the text find->replace layer.
     */
    private static String calloutId(CalloutEvent ev) {
        final CalloutTraceInfo trace = ev.getTrace();
        if (trace instanceof ModifiableCalloutTraceInfo mti) {
            return idForField(mti.getCalloutField());
        }
        return null;
    }

    /**
     * Handle a NyaaTriggers control command (one JSON line on stdin):
     *   {"nyaa_cmd":"set_callout","id":..,"tts":..,"text":..,"enable":bool}
     *   {"nyaa_cmd":"reset_callout","id":..}
     * set_callout edits the live engine's own TTS/visual/enable settings for that
     * trigger (so the change applies immediately, with tokens still substituted);
     * reset_callout reverts to the trigger's defaults. Each setting write is guarded
     * so a read-only persistence backend cannot abort the others (the in-memory value
     * is updated before the optional file write, so the live engine always sees it).
     */
    private static void handleCommand(String line) {
        try {
            final JsonNode n = MAPPER.readTree(line);
            final String cmd = n.path("nyaa_cmd").asText("");
            // Automark control is not keyed by a callout id, so dispatch it first.
            if ("set_automark".equals(cmd)) {
                handleAutomark(n);
                return;
            }
            final String id = n.path("id").asText(null);
            final ModifiedCalloutHandle h = (id == null) ? null : CALLOUTS.get(id);
            if (h == null) {
                diag("command: unknown callout id " + id);
                return;
            }
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
        } catch (Throwable t) {
            diag("command error: " + t);
        }
    }

    private static void trySet(Runnable r) {
        try {
            r.run();
        } catch (Throwable t) {
            diag("setting write skipped: " + t);   // in-memory value already updated
        }
    }

    /**
     * Enable/disable Telesto automarking and/or point the engine at the user's Telesto
     * plugin, from a NyaaTriggers control line:
     *   {"nyaa_cmd":"set_automark","enable":bool,"uri":"http://localhost:45678/"}
     * enable=true selects the "telesto-am" service; enable=false selects "none" (which
     * also de-selects the harmful default keyboard-macro handler). Both writes are
     * in-memory-effective even under the read-only persistence backend (see trySet).
     */
    private static void handleAutomark(JsonNode n) {
        if (n.hasNonNull("uri") && TELESTO != null) {
            trySet(() -> TELESTO.getUriSetting().set(URI.create(n.get("uri").asText().trim())));
        }
        if (n.has("enable")) {
            applyAutomark(n.get("enable").asBoolean(true));
        }
        diag("set_automark applied");
    }

    /**
     * Turn Telesto automarking on/off as one coupled operation:
     *  - party polling (telesto-support.pull-party-list) is tied to the feature, so when
     *    OFF the engine makes ZERO HTTP contact with the Telesto plugin (no GetPartyMembers
     *    POSTs on zone/combat ticks), and when ON it polls GetPartyMembers - which is
     *    REQUIRED for the game-correct party-slot order (without it slots are job-sorted
     *    and "/mk attack <slot>" can mark the wrong player). We force it on/off regardless
     *    of the user's ~/.triggevent value.
     *  - the actuation service is set to "telesto-am" (on) or "none" (off, which also
     *    de-selects the harmful default keyboard-macro AWT-Robot handler).
     *  - on enable, a party-order refresh is requested immediately so the first marks
     *    resolve to the game order rather than the job-sorted fallback (narrows, but does
     *    not fully close, the post-(re)start window before the first GetPartyMembers reply).
     */
    private static void applyAutomark(boolean enable) {
        if (TELESTO != null) {
            trySet(() -> TELESTO.getEnablePartyList().set(enable));
        }
        selectAutomarkService(enable ? "telesto-am" : "none");
        if (enable && TELESTO != null) {
            trySet(TELESTO::refreshPartyIfEnabled);   // request game party order now
        }
    }

    /**
     * Select an automark actuation service by its ServiceDescriptor id ("telesto-am" /
     * "none"). Selecting by id (not priority) is deliberate so an upstream priority
     * change can't silently re-select the wrong actuator. No-op if the engine has no
     * telesto-core (AM_SELECTOR null) or the id isn't registered.
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

    /** canonicalClassName.fieldName - a stable id shared by the inventory and each callout. */
    private static String idForField(Field f) {
        if (f == null) {
            return null;
        }
        final String cn = f.getDeclaringClass().getCanonicalName();
        return cn == null ? null : cn + '.' + f.getName();
    }

    /**
     * One-shot startup catalog of every modifiable callout (the read-only rows in the
     * unified Triggers tab), grouped by fight. Emits one line:
     *   {"t":"inventory","triggers":[{"id":..,"name":..,"fight":..,"group":..,"text":..},..]}
     * "fight" is the KnownDuty enum constant (e.g. "FRU","DMU","None") for the Python
     * side to map; "name" is the callout description; "text" is its default callout text.
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
        for (CalloutGroup g : groups) {
            final String fight = g.getDuty() == null ? "None" : g.getDuty().name();
            final String groupName = g.getName();
            for (ModifiedCalloutHandle h : g.getCallouts()) {
                final String id = idForField(h.getField());
                if (id == null) {
                    continue;                          // can't address it -> can't disable it -> skip
                }
                CALLOUTS.put(id, h);                    // addressable for set_callout/reset_callout
                String text = h.getOriginal().getOriginalVisualText();
                if (text == null || text.isEmpty()) {
                    text = h.getOriginal().getOriginalTts();
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append("{\"id\":\"").append(esc(id)).append('"');   // id is always present here
                field(sb, "name", h.getDescription());
                field(sb, "fight", fight);
                field(sb, "group", groupName);
                field(sb, "text", text);
                sb.append('}');
            }
        }
        sb.append("]}");
        println(sb.toString());
        diag("inventory emitted: " + groups.size() + " groups");
    }

    /**
     * Triggevent has no first-class severity enum; urgency is encoded via callout
     * color. Heuristic: strongly-red override -> alarm, any other override -> alert,
     * no override -> info. (NyaaTriggers maps these to gold / peach / red.)
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

    // ── tiny JSON writer (no dependency on the engine's Jackson version) ──────────
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
}
