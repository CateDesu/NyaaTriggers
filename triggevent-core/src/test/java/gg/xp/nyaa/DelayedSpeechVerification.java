package gg.xp.nyaa;

import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.callouts.CalloutDefaults;
import gg.xp.xivsupport.callouts.ModifiableCallout;
import gg.xp.xivsupport.callouts.ModifiedCalloutHandle;
import gg.xp.xivsupport.persistence.InMemoryMapPersistenceProvider;
import gg.xp.xivsupport.speech.ModifiableCalloutTraceInfo;
import gg.xp.xivsupport.events.actlines.events.WipeEvent;
import gg.xp.xivsupport.replay.PullRecovery;
import gg.xp.xivsupport.speech.BasicCalloutEvent;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public final class DelayedSpeechVerification {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final ByteArrayOutputStream OUTPUT = new ByteArrayOutputStream();
    private static EventMaster master;
    private static PullRecovery recovery;
    private static Instant start;
    private static java.lang.reflect.Method command;
    private static final ModifiableCallout<Object> tunable = new ModifiableCallout<>("Test", "Configured speech");
    private static final String ID = DelayedSpeechVerification.class.getCanonicalName() + ".tunable";

    public static void main(String[] args) throws Exception {
        PrintStream console = System.out;
        System.setOut(new PrintStream(OUTPUT, true, StandardCharsets.UTF_8));
        try {
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            var pico = (MutablePicoContainer) boot.invoke(null);
            master = pico.getComponent(EventMaster.class);
            var field = TriggeventCore.class.getDeclaredField("RECOVERY");
            field.setAccessible(true);
            recovery = (PullRecovery) field.get(null);
            command = TriggeventCore.class.getDeclaredMethod("handleCommand", JsonNode.class);
            command.setAccessible(true);
            var handle = ModifiedCalloutHandle.installHandle(
                    DelayedSpeechVerification.class.getDeclaredField("tunable"), tunable,
                    new InMemoryMapPersistenceProvider(), "delay-test", null, null, CalloutDefaults.dummy());
            var handles = TriggeventCore.class.getDeclaredField("CALLOUTS");
            handles.setAccessible(true);
            ((Map<String, ModifiedCalloutHandle>) handles.get(null)).put(ID, handle);
            start = Instant.now().plusSeconds(120);
            recovery.begin(start.toString());
            recovery.end();

            int before = calls().size();
            var delayed = call("Later speech", "Popup now", 2000);
            master.pushEventAndWait(delayed);
            var initial = calls().subList(before, calls().size());
            check(initial.size() == 1, "Delayed callout did not show exactly one popup");
            check("Popup now".equals(initial.get(0).path("text").asText()), "Popup text changed");
            check(initial.get(0).path("tts").asText("").isEmpty(), "Speech was sent before its native delay");
            recovery.advance(start.plusMillis(1000));
            check(calls().size() == before + 1, "Speech arrived before its deadline");
            recovery.advance(start.plusMillis(2500));
            var later = calls().subList(before + 1, calls().size());
            check(later.size() == 1, "Delayed speech was missing or duplicated");
            check("Later speech".equals(later.get(0).path("tts").asText()), "Delayed speech changed");
            check(later.get(0).path("tts_only").asBoolean(false), "Delayed speech would repeat the popup");

            before = calls().size();
            master.pushEventAndWait(call("Immediate", "Immediate", 0));
            recovery.advance(start.plusMillis(3000));
            check(calls().size() == before + 1, "Immediate speech was duplicated");
            check("Immediate".equals(calls().get(before).path("tts").asText()), "Immediate speech was deferred");

            before = calls().size();
            master.pushEventAndWait(call("Old pull", "Old pull popup", 2000));
            master.pushEventAndWait(new WipeEvent());
            recovery.advance(start.plusMillis(5500));
            check(calls().size() == before + 1, "Wipe left delayed speech alive");

            before = calls().size();
            var previous = call("Old mechanic", "Old mechanic popup", 2000);
            master.pushEventAndWait(previous);
            var replacement = call("New mechanic", "New mechanic popup", 0);
            replacement.setReplaces(previous);
            master.pushEventAndWait(replacement);
            recovery.advance(start.plusMillis(8000));
            check(calls().size() == before + 2, "Replaced speech still ran");

            before = calls().size();
            master.pushEventAndWait(call("Speech fallback", null, 2000));
            check("Speech fallback".equals(calls().get(before).path("text").asText()),
                    "Native overlay disabled removed the fallback popup");
            advance(10500);
            check(calls().size() == before + 2, "Fallback delayed speech was missing");

            feed(Map.of("type", "ChangeZone", "zoneID", 123, "zoneName", "Test"));
            before = calls().size();
            master.pushEventAndWait(call("Same zone", "Popup", 2000));
            feed(Map.of("type", "ChangeZone", "zoneID", 123, "zoneName", "Test renamed"));
            command(Map.of("nyaa_cmd", "set_automark", "enable", false));
            advance(13000);
            check(calls().size() == before + 2, "Unchanged zone or automark settings canceled speech");
            before = calls().size();
            master.pushEventAndWait(call("Old zone", "Popup", 2000));
            feed(Map.of("type", "ChangeZone", "zoneID", 124, "zoneName", "Another"));
            advance(15500);
            check(calls().size() == before + 1, "Zone change left delayed speech alive");

            before = calls().size();
            master.pushEventAndWait(configured());
            command(Map.of("nyaa_cmd", "set_callout", "id", ID, "tts", "Configured speech", "enable", true));
            advance(18000);
            check(calls().size() == before + 2, "Unchanged callout settings canceled speech");
            before = calls().size();
            master.pushEventAndWait(configured());
            command(Map.of("nyaa_cmd", "set_callout", "id", ID, "enable", false));
            command(Map.of("nyaa_cmd", "set_callout", "id", ID, "enable", true));
            advance(20500);
            check(calls().size() == before + 1, "Disable and reenable resurrected old speech");
            before = calls().size();
            master.pushEventAndWait(configured());
            command(Map.of("nyaa_cmd", "set_callout", "id", ID, "tts", "Changed speech"));
            command(Map.of("nyaa_cmd", "reset_callout", "id", ID));
            advance(23000);
            check(calls().size() == before + 1, "Editing and resetting resurrected old speech");

            before = calls().size();
            master.pushEventAndWait(configured());
            master.pushEventAndWait(call("Other trigger", "Other popup", 1000));
            command(Map.of("nyaa_cmd", "cancel_speech", "ids", List.of(ID), "token", 7));
            advance(24100);
            check(calls().size() == before + 3, "ID cancellation removed unrelated speech");
            advance(25200);
            check(calls().size() == before + 3, "UI cancellation left old speech pending");
            check(handle.isTtsEffectivelyEnabled(), "UI cancellation changed native author enable defaults");
            check(OUTPUT.toString(StandardCharsets.UTF_8).contains("\"t\":\"speech_canceled\",\"token\":7"),
                    "UI cancellation did not acknowledge its output barrier");
            master.pushEventAndWait(configured());
            advance(27400);
            check(calls().size() == before + 5, "UI cancellation suppressed fresh speech");

            before = calls().size();
            var expired = new BasicCalloutEvent("Short visual", "Popup", 100);
            expired.setTtsDelayMs(2000);
            master.pushEventAndWait(expired);
            advance(29600);
            check(calls().size() == before + 2, "Visual expiry canceled valid delayed speech");
            before = calls().size();
            master.pushEventAndWait(call("Before recovery", "Popup", 2000));
            command(Map.of("nyaa_cmd", "pause_feed"));
            master.pushEventAndWait(call("Historical", "Hidden", 4000));
            advance(35000);
            recovery.end();
            advance(36000);
            check(calls().size() == before + 1, "Recovery leaked old or historical speech");

            before = calls().size();
            command(Map.of("nyaa_cmd", "recover_begin", "time", start.plusMillis(36000).toString()));
            master.pushEventAndWait(call("After handoff", "Historical popup", 2000));
            advance(37000);
            recovery.end();
            advance(38500);
            check(calls().size() == before + 1, "Future speech was lost at recovery handoff");
            check("After handoff".equals(calls().get(before).path("tts").asText())
                    && calls().get(before).path("tts_only").asBoolean(false),
                    "Recovery replayed the popup or changed future speech");

            feed(Map.of("type", "ChangePrimaryPlayer", "charID", 0x10000001, "charName", "Player"));
            feed(Map.of("rseq", "allCombatants", "combatants", List.of(
                    Map.of("ID", 0x10000001, "Name", "Player", "Type", 1, "Job", 21),
                    Map.of("ID", 0x40000001, "Name", "Boss", "Type", 2))));
            master.pushEventAndWait(new gg.xp.xivsupport.events.actlines.events.actorcontrol.FadeInEvent());
            pico.getComponent(gg.xp.reevent.events.EventDistributor.class).registerHandler(
                    gg.xp.xivsupport.events.actlines.events.AbilityUsedEvent.class,
                    (context, event) -> context.accept(call("First combat ability", "First popup", 2000)));
            var state = pico.getComponent(gg.xp.xivsupport.events.state.XivState.class);
            before = calls().size();
            master.pushEventAndWait(new gg.xp.xivsupport.events.actlines.events.AbilityUsedEvent(
                    new gg.xp.xivsupport.models.XivAbility(0x123), state.getPlayer(),
                    state.getCombatant(0x40000001), List.of(), 1, 0, 1));
            check(pico.getComponent(gg.xp.xivsupport.events.misc.pulls.PullTracker.class).getCurrentStatus()
                    == gg.xp.xivsupport.events.misc.pulls.PullStatus.COMBAT,
                    "First ability did not start native combat tracking");
            advance(41000);
            check(calls().size() == before + 2, "First combat ability lost its delayed speech");

            before = calls().size();
            master.pushEventAndWait(configured());
            master.pushEventAndWait(call("Unidentified old speech", "Popup", 1000));
            command(Map.of("nyaa_cmd", "cancel_speech", "all", true, "token", 8));
            advance(44000);
            check(calls().size() == before + 2, "Mode cancellation retained known or unidentified speech");
            check(handle.isTtsEffectivelyEnabled(), "Mode cancellation changed native author enable defaults");
            master.pushEventAndWait(configured());
            master.pushEventAndWait(call("Unidentified fresh speech", "Popup", 1000));
            advance(47000);
            check(calls().size() == before + 6, "Mode cancellation suppressed fresh speech");
            verifyFormattingOrder();

            var pendingField = TriggeventCore.class.getDeclaredField("PENDING_SPEECH");
            pendingField.setAccessible(true);
            var pending = (Map<?, ?>) pendingField.get(null);
            var receive = TriggeventCore.class.getDeclaredMethod("onCallout",
                    gg.xp.reevent.events.EventContext.class, gg.xp.xivsupport.speech.CalloutEvent.class);
            receive.setAccessible(true);
            for (int i = 0; i < 1030; i++) {
                var event = call("Never scheduled", "Popup", 2000);
                receive.invoke(null, null, event);
            }
            check(pending.size() == 1024, "Pending speech was not bounded");
            master.pushEventAndWait(new WipeEvent());
            check(pending.isEmpty(), "Reset retained pending speech with missing native requests");

            console.println("VERIFIED native speech delay immediate popup fallback zero delay and single delivery");
            console.println("VERIFIED wipe replacement visual expiry zone recovery settings and bounded pending speech");
            console.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            console.println(OUTPUT.toString(StandardCharsets.UTF_8));
            error.printStackTrace(console);
            System.exit(1);
        }
    }

    private static BasicCalloutEvent call(String tts, String text, int delay) {
        var call = new BasicCalloutEvent(tts, text, 15000);
        call.setTtsDelayMs(delay);
        return call;
    }

    private static void advance(long millis) {
        recovery.advance(start.plusMillis(millis));
    }

    private static BasicCalloutEvent configured() {
        var event = call("Configured speech", "Popup", 2000);
        event.setTrace(new ModifiableCalloutTraceInfo(tunable.getModified()));
        return event;
    }

    private static void verifyFormattingOrder() throws Exception {
        var receive = TriggeventCore.class.getDeclaredMethod("onCallout",
                gg.xp.reevent.events.EventContext.class, gg.xp.xivsupport.speech.CalloutEvent.class);
        receive.setAccessible(true);
        for (boolean all : List.of(false, true)) {
            int offset = OUTPUT.size();
            var entered = new CountDownLatch(1);
            var release = new CountDownLatch(1);
            var acknowledged = new CountDownLatch(1);
            var event = new gg.xp.xivsupport.speech.ProcessedCalloutEvent(
                    new gg.xp.xivsupport.callouts.CalloutTrackingKey(), "Formatting speech", () -> {
                entered.countDown();
                try { release.await(5, TimeUnit.SECONDS); }
                catch (InterruptedException e) { throw new RuntimeException(e); }
                return "Formatting popup";
            }, () -> false, () -> null, null, null);
            event.setTrace(new ModifiableCalloutTraceInfo(tunable.getModified()));
            var emitter = new Thread(() -> {
                try { receive.invoke(null, null, event); }
                catch (Exception e) { throw new RuntimeException(e); }
            });
            emitter.start();
            check(entered.await(5, TimeUnit.SECONDS), "Visual formatter never ran");
            var canceler = new Thread(() -> {
                try {
                    command(all ? Map.of("nyaa_cmd", "cancel_speech", "all", true, "token", 91)
                            : Map.of("nyaa_cmd", "cancel_speech", "ids", List.of(ID), "token", 91));
                } catch (Exception e) { throw new RuntimeException(e); }
                finally { acknowledged.countDown(); }
            });
            canceler.start();
            acknowledged.await(300, TimeUnit.MILLISECONDS);
            release.countDown();
            emitter.join(5000);
            canceler.join(5000);
            check(!emitter.isAlive() && !canceler.isAlive(), "Formatting cancellation did not finish");
            String result = OUTPUT.toString(StandardCharsets.UTF_8).substring(offset);
            int speech = result.indexOf("\"tts\":\"Formatting speech\"");
            int ack = result.indexOf("\"t\":\"speech_canceled\"");
            check(speech >= 0 && ack > speech, "Old formatted speech followed cancellation acknowledgement");
        }
    }

    private static void command(Map<String, ?> value) throws Exception {
        command.invoke(null, MAPPER.valueToTree(value));
    }

    private static void feed(Map<String, ?> value) {
        String json = MAPPER.writeValueAsString(value);
        recovery.feed(json, MAPPER.readTree(json));
    }

    private static List<JsonNode> calls() {
        var calls = new ArrayList<JsonNode>();
        for (String line : OUTPUT.toString(StandardCharsets.UTF_8).split("\\R")) {
            if (line.startsWith("{\"t\":\"callout\"")) {
                calls.add(MAPPER.readTree(line));
            }
        }
        return calls;
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }
}
