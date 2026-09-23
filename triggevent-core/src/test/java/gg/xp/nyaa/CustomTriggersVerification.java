package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.events.actlines.events.AbilityCastStart;
import gg.xp.xivsupport.events.actlines.events.BuffApplied;
import gg.xp.xivsupport.events.actlines.events.BuffRemoved;
import gg.xp.xivsupport.events.actlines.events.WipeEvent;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.models.XivAbility;
import gg.xp.xivsupport.models.XivStatusEffect;
import gg.xp.xivsupport.replay.PullRecovery;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.ObjectMapper;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

public final class CustomTriggersVerification {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final List<String> calls = new CopyOnWriteArrayList<>();
    private static EventMaster master;
    private static PullRecovery recovery;
    private static XivState state;
    private static String definition;
    private static java.lang.reflect.Method command;
    private static final class Tick extends BaseEvent {}

    public static void main(String[] args) throws Exception {
        try {
            definition = Files.readString(Path.of(args[0]));
            for (var test : MAPPER.readTree(Files.readString(Path.of(args[1])))) {
                var config = new CustomTriggers.Configure(MAPPER.createArrayNode().add(test.path("definition")));
                new CustomTriggers(null, null).handle(null, config);
                check((config.error == null) == test.path("valid").asBoolean(false),
                        test.path("label").asText() + ": " + config.error);
            }
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            var pico = (MutablePicoContainer) boot.invoke(null);
            var recoveryField = TriggeventCore.class.getDeclaredField("RECOVERY");
            recoveryField.setAccessible(true);
            recovery = (PullRecovery) recoveryField.get(null);
            master = pico.getComponent(EventMaster.class);
            state = pico.getComponent(XivState.class);
            command = TriggeventCore.class.getDeclaredMethod("handleCommand", tools.jackson.databind.JsonNode.class);
            command.setAccessible(true);
            pico.getComponent(EventDistributor.class).registerHandler(CustomTriggers.Callout.class,
                    (context, event) -> calls.add(event.getCallText()));
            feed(Map.of("type", "ChangeZone", "zoneID", 123, "zoneName", "Test"));
            feed(Map.of("type", "ChangePrimaryPlayer", "charID", 0x10000001, "charName", "Player"));
            feed(Map.of("rseq", "allCombatants", "combatants", List.of(
                    Map.of("ID", 0x10000001, "Name", "Player", "Type", 1, "Job", 21),
                    Map.of("ID", 0x40000001, "Name", "Boss", "Type", 2))));

            configure(definition);
            buff(0xA123);
            feed(Map.of("type", "ChangeZone", "zoneID", 123, "zoneName", "Test"));
            configure(definition);
            cast();
            check(calls.equals(List.of("Player, go left")), "First event was not retained across unchanged config and zone");
            buff(0xA124);
            cast();
            check(calls.equals(List.of("Player, go left", "Player, go right")), "Alternative branch failed");

            buff(0xA123);
            master.pushEventAndWait(new WipeEvent());
            cast();
            check(calls.size() == 2, "Wipe did not cancel the sequence");
            buff(0xA123);
            configure(null);
            cast();
            check(calls.size() == 2, "Removing a definition did not cancel its wait");

            configure(definition);
            buff(0xA123);
            configure(definition.replace("go left", "changed"));
            cast();
            check(calls.size() == 2, "Editing left an old wait alive");
            buff(0xA123);
            cast();
            check(calls.get(2).equals("Player, changed"), "Edited callout did not run");

            configure(definition.replace("start_id", "my_status"));
            buff(0xA123);
            master.pushEventAndWait(new BuffRemoved(new XivStatusEffect(0xA123), 0,
                    state.getCombatant(0x40000001), state.getPlayer(), 0));
            cast();
            check(calls.get(3).equals("Player, go right"), "Current status condition used the starting status");

            configure(definition.replace("\"zone_id\": 123", "\"zone_id\": 124"));
            buff(0xA123);
            cast();
            check(calls.size() == 4, "Wrong zone started the sequence");

            configure(definition.replace("120.0", "1.0"));
            buff(0xA123);
            Thread.sleep(1100);
            cast();
            check(calls.size() == 4, "Expired sequence called out");

            configure(definition);
            configure(definition.replace("start_id", "unsupported"));
            buff(0xA123);
            cast();
            check(calls.get(4).equals("Player, go left"), "Invalid replacement removed a working definition");

            String delayed = definition.replace("{\"kind\": \"callout\"",
                    "{\"kind\":\"delay\",\"seconds\":0.1}, {\"kind\": \"callout\"");
            configure(delayed);
            buff(0xA123);
            cast();
            check(calls.size() == 5, "Delay was skipped");
            Thread.sleep(250);
            master.pushEventAndWait(new Tick());
            check(calls.size() == 6, "Delayed callout did not run");
            buff(0xA123);
            cast();
            master.pushEventAndWait(new WipeEvent());
            Thread.sleep(250);
            master.pushEventAndWait(new Tick());
            check(calls.size() == 6, "Wipe left a delayed callout alive");

            configure(definition);
            recovery.begin(Instant.now().minusSeconds(5).toString());
            buff(0xA124);
            System.out.println("HISTORY_BOUNDARY");
            recovery.advance(Instant.now());
            recovery.end();
            cast();
            check(calls.get(6).equals("Player, go right"), "Recovery did not restore a pending sequence");
            configure(definition.replace("A123 | A124", "A123\u00a0|\u202fA124"));
            feed(Map.of("type", "LogLine", "rawLine", "26|" + Instant.now()
                    + "|A123|Debuff|30|40000001|Boss|10000001|Player|0|0"));
            feed(Map.of("type", "LogLine", "rawLine", "20|" + Instant.now()
                    + "|40000001|Boss|B123|Cast|10000001|Player|3|100|100|0|0|0"));
            check(calls.size() == 8 && calls.get(7).equals("Player, go left"),
                    "Raw log events did not match IDs with pasted spacing");
            System.out.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            error.printStackTrace();
            System.exit(1);
        }
    }

    private static void configure(String json) throws Exception {
        command.invoke(null, MAPPER.readTree("{\"nyaa_cmd\":\"custom_triggers\",\"triggers\":["
                + (json == null ? "" : json) + "]}"));
        master.pushEventAndWait(new Tick());
    }

    private static void feed(Map<String, ?> message) {
        String json = MAPPER.writeValueAsString(message);
        recovery.feed(json, MAPPER.readTree(json));
    }

    private static void buff(long id) {
        master.pushEventAndWait(new Tick());
        master.pushEventAndWait(new BuffApplied(new XivStatusEffect(id), 30,
                state.getCombatant(0x40000001), state.getPlayer(), 0));
    }

    private static void cast() {
        master.pushEventAndWait(new AbilityCastStart(new XivAbility(0xB123),
                state.getCombatant(0x40000001), state.getPlayer(), 3));
        master.pushEventAndWait(new Tick());
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message + ": " + calls);
    }
}
