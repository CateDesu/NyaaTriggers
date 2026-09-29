package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.events.actlines.events.*;
import gg.xp.xivsupport.events.triggers.seq.SequentialTrigger;
import gg.xp.xivsupport.events.triggers.seq.SequentialTriggerFailedEvent;
import gg.xp.xivsupport.models.XivAbility;
import gg.xp.xivsupport.models.XivCombatant;
import gg.xp.xivsupport.models.XivStatusEffect;
import gg.xp.xivsupport.replay.PullRecovery;
import gg.xp.xivsupport.speech.BasicCalloutEvent;
import gg.xp.xivsupport.speech.CalloutEvent;
import gg.xp.xivsupport.speech.CalloutTraceInfo;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

public final class DiagnosticsVerification {
    private static final ByteArrayOutputStream OUTPUT = new ByteArrayOutputStream();
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String SECRET = "PrivateSentinel /home/PrivateUser token=SecretValue";

    public static void main(String[] args) throws Exception {
        PrintStream console = System.out;
        System.setOut(new PrintStream(OUTPUT, true, StandardCharsets.UTF_8));
        try {
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            var pico = (MutablePicoContainer) boot.invoke(null);
            var inventory = TriggeventCore.class.getDeclaredMethod("emitInventory", MutablePicoContainer.class);
            inventory.setAccessible(true);
            inventory.invoke(null, pico);
            EventMaster master = pico.getComponent(EventMaster.class);
            EventDistributor dist = pico.getComponent(EventDistributor.class);
            XivCombatant player = new XivCombatant(0x10031A09, SECRET, true, true,
                    1, null, null, null, 0, 0, 1, 100, 0, 0);
            XivCombatant boss = new XivCombatant(0x400ABCDE, SECRET);
            AbilityCastStart cast = new AbilityCastStart(new XivAbility(1234, SECRET), boss, player, 5.0);
            SequentialTrigger<BaseEvent> broken = new SequentialTrigger<>(5000, BaseEvent.class,
                    event -> event == cast, (event, controller) -> {
                        controller.waitEvent(BuffApplied.class);
                        throw new IllegalStateException(SECRET);
                    });
            broken.setHandlerName("DMU.ttSq");
            dist.registerHandler(BaseEvent.class, broken::feed);
            master.pushEventAndWait(cast);
            master.pushEventAndWait(new BuffApplied(new XivStatusEffect(2468, SECRET), 12.5, boss, player, 2));
            master.pushEventAndWait(new TetherEvent(boss, player, 45));
            master.pushEventAndWait(new HeadMarkerEvent(player, 675));
            master.pushEventAndWait(new EntityKilledEvent(boss, player));
            master.pushEventAndWait(new SequentialTriggerFailedEvent(SECRET, SECRET, cast,
                    new IllegalArgumentException(SECRET)));
            master.pushEventAndWait(new SequentialTriggerFailedEvent("DMU.ttSq", "BuffApplied in quick succession", cast,
                    new IllegalStateException(SECRET)));
            master.pushEventAndWait(new BasicCalloutEvent(SECRET, SECRET));
            var diagnosticField = TriggeventCore.class.getDeclaredField("diagnostics");
            diagnosticField.setAccessible(true);
            ((EngineDiagnostics) diagnosticField.get(null)).pipeline();

            List<JsonNode> records = records();
            check(records.stream().anyMatch(record -> kind(record, "engine_runtime")), "Missing runtime metadata");
            List<JsonNode> failures = records.stream().filter(record -> kind(record, "sequence_failed")).toList();
            check(failures.size() == 3, "Failures missing or duplicated");
            JsonNode failure = failures.get(0);
            check(failure.path("trigger_class").asText("").equals("gg.xp.xivsupport.triggers.ultimate.DMU"), "Missing built-in trigger class");
            check(failure.path("trigger_field").asText("").equals("ttSq"), "Missing built-in trigger field");
            check(failure.path("error_type").asText("").equals("java.lang.IllegalStateException"), "Missing error type");
            check(failure.path("frames").size() > 0, "Missing engine stack locations");
            check(!failures.get(1).has("trigger_class") && !failures.get(1).has("trigger_field"), "Custom name leaked");
            check(failures.get(2).path("wait_event").asText("").equals("gg.xp.xivsupport.events.actlines.events.BuffApplied")
                    && failures.get(2).path("wait_kind").asText("").equals("burst"), "Missing wait context");
            JsonNode tether = records.stream().filter(record -> record.path("event_kind").asText("").equals("tether")).findFirst().orElseThrow();
            check(tether.path("game_id").asLong() == 45 && tether.path("on_player").asBoolean(), "Tether context missing");
            check(tether.path("source_actor").asLong() == 1 && tether.path("target_actor").asLong() == 2, "Actors not tokenized");
            JsonNode buff = records.stream().filter(record -> record.path("event_kind").asText("").equals("buff_add")).findFirst().orElseThrow();
            check(buff.path("duration_ms").asLong() == 12500 && buff.path("stacks").asInt() == 2, "Buff context missing");
            JsonNode totals = records.stream().filter(record -> kind(record, "engine_pipeline")).reduce((a, b) -> b).orElseThrow();
            check(totals.path("counters").path("failures").asInt() == 3, "Failure total missing");
            check(records.stream().anyMatch(record -> kind(record, "engine_callout") && record.path("seq").asLong() > 0), "Missing callout correlation");
            String serialized = MAPPER.writeValueAsString(records);
            for (String forbidden : List.of("PrivateSentinel", "PrivateUser", "SecretValue", Long.toString(player.getId()), Long.toString(boss.getId()))) {
                check(!serialized.contains(forbidden), "Private value in diagnostic records");
            }
            long before = records.stream().filter(record -> kind(record, "engine_event")).count();
            var recoveryField = TriggeventCore.class.getDeclaredField("RECOVERY");
            recoveryField.setAccessible(true);
            PullRecovery recovery = (PullRecovery) recoveryField.get(null);
            recovery.begin(Instant.now().toString());
            master.pushEventAndWait(new BuffApplied(new XivStatusEffect(2468, SECRET), 12.5, boss, player, 2));
            ((EngineDiagnostics) diagnosticField.get(null)).pipeline();
            check(records().stream().filter(record -> kind(record, "engine_event")).count() == before,
                    "Historical mechanics flooded diagnostics");
            JsonNode replayTotals = records().stream().filter(record -> kind(record, "engine_pipeline")).reduce((a, b) -> b).orElseThrow();
            check(replayTotals.path("replaying").asBoolean() && replayTotals.path("counters").path("buffs").asInt() == 2,
                    "Replay counters missing");
            recovery.end();
            master.pushEventAndWait(new TetherEvent(boss, player, 45));
            check(records().stream().filter(record -> kind(record, "engine_event")).count() == before + 1,
                    "Live diagnostics did not resume after recovery");
            // A broken diagnostics sink cannot stop mechanic handling or callouts.
            EngineDiagnostics unavailable = new EngineDiagnostics(null, record -> { throw new IllegalStateException(SECRET); });
            unavailable.error("feed", new IllegalStateException(SECRET));
            unavailable.callout(null, 1, false, true, true, false);
            verifyCalloutEvaluation();
            console.println("VERIFIED structured engine failures and code locations");
            console.println("VERIFIED mechanic inputs and anonymous actor correlation");
            console.println("VERIFIED diagnostic privacy and failed sink isolation");
            console.println("VERIFIED diagnostics do not reevaluate callout predicates");
            console.println("VERIFIED bounded replay diagnostics and live handoff");
            console.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            error.printStackTrace(console);
            System.exit(1);
        }
    }

    private static void verifyCalloutEvaluation() throws Exception {
        var emit = TriggeventCore.class.getDeclaredMethod("emitCallout", CalloutEvent.class,
                String.class, String.class, boolean.class, Instant.class);
        emit.setAccessible(true);
        int[] expiryCalls = {0}, traceCalls = {0};
        BasicCalloutEvent expiry = new BasicCalloutEvent("expiry probe") {
            @Override
            public boolean isNaturallyExpired() {
                if (++expiryCalls[0] > 1) throw new AssertionError("Expiry evaluated twice");
                return false;
            }
        };
        BasicCalloutEvent trace = new BasicCalloutEvent("trace probe") {
            @Override
            public CalloutTraceInfo getTrace() {
                if (++traceCalls[0] > 1) throw new AssertionError("Trace evaluated twice");
                return null;
            }
        };
        long before = calloutCount();
        emit.invoke(null, expiry, "expiry probe", "expiry probe", false, Instant.now());
        emit.invoke(null, trace, "trace probe", "trace probe", false, Instant.now());
        long delivered = calloutCount() - before;
        check(delivered == 2 && expiryCalls[0] == 1 && traceCalls[0] == 1,
                "Diagnostics changed delivery: emitted=" + delivered
                        + ", expiry calls=" + expiryCalls[0] + ", trace calls=" + traceCalls[0]);
    }

    private static long calloutCount() {
        return OUTPUT.toString(StandardCharsets.UTF_8).lines()
                .filter(line -> line.startsWith("{"))
                .map(MAPPER::readTree)
                .filter(value -> value.path("t").asText("").equals("callout")).count();
    }

    private static List<JsonNode> records() {
        List<JsonNode> records = new ArrayList<>();
        for (String line : OUTPUT.toString(StandardCharsets.UTF_8).split("\\R")) {
            if (!line.startsWith("{")) continue;
            JsonNode value = MAPPER.readTree(line);
            if (value.path("t").asText("").equals("diagnostic")) records.add(value);
        }
        return records;
    }

    private static boolean kind(JsonNode record, String event) {
        return record.path("event").asText("").equals(event);
    }

    private static void check(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }
}
