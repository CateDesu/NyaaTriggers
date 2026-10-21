package gg.xp.nyaa;

import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.callouts.ModifiableCallout;
import gg.xp.xivsupport.callouts.ModifiedCalloutHandle;
import gg.xp.xivsupport.callouts.ModifiedCalloutRepository;
import gg.xp.xivsupport.events.actlines.events.HeadMarkerEvent;
import gg.xp.xivsupport.models.XivCombatant;
import gg.xp.xivsupport.replay.PullRecovery;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.lang.reflect.Method;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

final class DisplayTextVerification {
    private static final String ID = "gg.xp.xivsupport.events.triggers.duties.Dragonsong.p1_firstCleaveMarker";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final EventMaster master;
    private final ModifiedCalloutHandle handle;
    private final Method command;
    private final Supplier<List<JsonNode>> calls;

    private DisplayTextVerification(MutablePicoContainer pico, EventMaster master, Method command,
                                    Supplier<List<JsonNode>> calls) throws Exception {
        this.master = master;
        this.command = command;
        this.calls = calls;
        handle = pico.getComponent(ModifiedCalloutRepository.class).getAllCallouts().stream()
                .flatMap(group -> group.getCallouts().stream())
                .filter(candidate -> ID.equals(candidate.getField().getDeclaringClass().getCanonicalName()
                        + "." + candidate.getField().getName()))
                .findFirst().orElseThrow();
        var inventory = TriggeventCore.class.getDeclaredMethod("emitInventory", MutablePicoContainer.class);
        inventory.setAccessible(true);
        inventory.invoke(null, pico);
    }

    static void verify(MutablePicoContainer pico, EventMaster master, PullRecovery recovery, Method command,
                       Supplier<List<JsonNode>> calls) throws Exception {
        var verification = new DisplayTextVerification(pico, master, command, calls);
        verification.verify();
        verification.verifyDelayedEdits(recovery);
    }

    private void verify() throws Exception {
        var nativeOverlay = handle.getAllTextEnabled();
        check(!nativeOverlay.get() && !nativeOverlay.isSet(), "Fresh native overlay was not disabled by default");
        AtomicInteger nativeOverlayWrites = new AtomicInteger();
        for (boolean overlayEnabled : List.of(false, true)) {
            if (nativeOverlay.get() != overlayEnabled) nativeOverlay.set(overlayEnabled);
            Runnable observer = nativeOverlayWrites::incrementAndGet;
            nativeOverlay.addListener(observer);
            try {
                for (boolean sameText : List.of(false, true)) {
                    handle.getSameText().set(sameText);
                    var baseline = fire("baseline");
                    check("Marker, First Set".equals(baseline.path("tts").asText("")), "Native fixture speech was missing");
                    command(Map.of("nyaa_cmd", "set_callout", "id", ID,
                            "text", "Visual {direction} for {event.target.name}"));
                    var visual = fire("north");
                    check("Marker, First Set".equals(visual.path("tts").asText("")), "Display edit changed native speech");
                    check("Visual north for Engine Target".equals(visual.path("text").asText("")),
                            "Display edit was ignored with native overlay=" + overlayEnabled + " and sameText=" + sameText);
                    check(handle.getSameText().get() == sameText, "Display edit changed the native same-text preference");
                    command(Map.of("nyaa_cmd", "set_callout", "id", ID,
                            "tts", "Spoken {direction} to {event.target.name}"));
                    var speech = fire("east");
                    check("Spoken east to Engine Target".equals(speech.path("tts").asText("")), "Native speech tokens were lost");
                    check("Visual east for Engine Target".equals(speech.path("text").asText("")), "Speech edit erased display text");
                    command(Map.of("nyaa_cmd", "set_callout", "id", ID, "tts", ""));
                    var silent = fire("south");
                    check(silent.path("tts").asText("").isEmpty(), "Display-only callout acquired speech");
                    check("Visual south for Engine Target".equals(silent.path("text").asText("")), "Display-only callout was lost");
                    command(Map.of("nyaa_cmd", "set_callout", "id", ID, "enable", false));
                    var disabled = fire("disabled");
                    check(disabled.path("tts").asText("").isEmpty() && disabled.path("text").asText("").isEmpty(),
                            "Display override bypassed the disabled native callout");
                    command(Map.of("nyaa_cmd", "set_callout", "id", ID, "enable", true));
                    var enabled = fire("west");
                    check(enabled.path("tts").asText("").isEmpty()
                                    && "Visual west for Engine Target".equals(enabled.path("text").asText("")),
                            "Reenabling lost display-only state");
                    command(Map.of("nyaa_cmd", "reset_callout", "id", ID));
                    var restored = fire("baseline");
                    check(restored.path("tts").equals(baseline.path("tts"))
                                    && restored.path("text").equals(baseline.path("text")),
                            "Reset did not restore the original native output");
                    check(nativeOverlay.get() == overlayEnabled && nativeOverlayWrites.get() == 0,
                            "Nyaa changed the native overlay visibility setting");
                }
                nativeOverlay.set(true);
                check(nativeOverlayWrites.get() == 1, "Native overlay observer did not see its positive control");
            }
            finally {
                nativeOverlay.removeListener(observer);
                nativeOverlayWrites.set(0);
            }
        }
        nativeOverlay.delete();
        handle.getSameText().delete();
        check(!nativeOverlay.get() && !nativeOverlay.isSet(), "Native overlay setup was not restored");
    }

    private void verifyDelayedEdits(PullRecovery recovery) throws Exception {
        Instant start = Instant.now().plusSeconds(30);
        recovery.begin(start.toString());
        recovery.end();
        command(Map.of("nyaa_cmd", "set_callout", "id", ID,
                "tts", "Delayed {direction} to {event.target.name}", "text", "Original visual {direction}"));
        int before = displayCalls().size();
        var first = fire("north", 2000);
        check("Original visual north".equals(first.path("text").asText(""))
                        && first.path("tts").asText("").isEmpty(),
                "Native delayed display did not arrive before speech");
        command(Map.of("nyaa_cmd", "set_callout", "id", ID, "text", "Changed visual {direction}"));
        recovery.advance(start.plusMillis(1000));
        check(displayCalls().size() == before + 1, "Display edit repeated the popup or sent early speech");
        recovery.advance(start.plusMillis(2500));
        checkDelayedSpeech(before, "Delayed north to Engine Target", "Display edit canceled pending speech");

        before = displayCalls().size();
        var second = fire("east", 2000);
        check("Changed visual east".equals(second.path("text").asText("")),
                "New callout did not use the edited display");
        String defaultText = handle.getOriginal().getOriginalVisualText();
        command(Map.of("nyaa_cmd", "set_callout", "id", ID, "text", defaultText));
        recovery.advance(start.plusMillis(3500));
        check(displayCalls().size() == before + 1, "Display reset repeated the popup or sent early speech");
        recovery.advance(start.plusMillis(5000));
        checkDelayedSpeech(before, "Delayed east to Engine Target", "Display reset canceled pending speech");
        var restored = fire("west");
        check(defaultText.equals(restored.path("text").asText(""))
                        && "Delayed west to Engine Target".equals(restored.path("tts").asText("")),
                "Display reset changed speech or did not restore default text");
        command(Map.of("nyaa_cmd", "reset_callout", "id", ID));
    }

    private void checkDelayedSpeech(int before, String expected, String message) {
        List<JsonNode> after = displayCalls();
        check(after.size() == before + 2, message);
        JsonNode speech = after.get(before + 1);
        check(expected.equals(speech.path("tts").asText(""))
                        && speech.path("tts_only").asBoolean(false) && !speech.has("text"),
                "Delayed speech changed or repeated the display");
    }

    private JsonNode fire(String direction) {
        return fire(direction, 0);
    }

    @SuppressWarnings("unchecked")
    private JsonNode fire(String direction, int delayMs) {
        int before = displayCalls().size();
        var event = new HeadMarkerEvent(new XivCombatant(0x10000001, "Engine Target"), 0xABC);
        var original = (ModifiableCallout<HeadMarkerEvent>) handle.getOriginal();
        var callout = original.getModified(event, Map.of("direction", direction));
        callout.setTtsDelayMs(delayMs);
        master.pushEventAndWait(callout);
        List<JsonNode> after = displayCalls();
        check(after.size() == before + 1, "Native callout was missing or duplicated");
        return after.get(before);
    }

    private void command(Map<String, ?> message) throws Exception {
        command.invoke(null, MAPPER.valueToTree(message));
    }

    private List<JsonNode> displayCalls() {
        return calls.get().stream().filter(message -> ID.equals(message.path("id").asText(""))).toList();
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
