package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.xivsupport.replay.PullRecovery;
import gg.xp.xivsupport.replay.PullHistoryReader;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.telestosupport.TelestoMain;
import gg.xp.xivsupport.callouts.RawModifiedCallout;
import gg.xp.xivsupport.events.actlines.events.BuffApplied;
import gg.xp.xivsupport.events.actlines.events.ZoneChangeEvent;
import gg.xp.xivsupport.events.actlines.events.WipeEvent;
import gg.xp.xivsupport.persistence.settings.IntSetting;
import gg.xp.xivsupport.events.delaytest.BaseDelayedEvent;
import gg.xp.xivsupport.events.state.RefreshSpecificCombatantsRequest;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.events.state.combatstate.StatusEffectRepository;
import gg.xp.xivsupport.events.triggers.seq.SequentialTriggerFailedEvent;
import gg.xp.xivsupport.events.triggers.seq.SequentialTrigger;
import gg.xp.xivsupport.events.misc.EchoEvent;
import gg.xp.xivsupport.events.triggers.marks.AutoMarkSlotRequest;
import gg.xp.xivsupport.speech.CalloutEvent;
import gg.xp.xivsupport.models.XivStatusEffect;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

public final class RecoveryVerification {
    private static final ObjectMapper mapper = new ObjectMapper();
    private static final class Timer extends BaseDelayedEvent {
        Timer(long delay) { super(delay); }
    }
    private static final class Tick extends BaseEvent {}
    private static final class Seed extends BaseEvent {}

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    public static void main(String[] args) throws Exception {
        try {
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            var pico = (MutablePicoContainer) boot.invoke(null);
            var field = TriggeventCore.class.getDeclaredField("RECOVERY");
            field.setAccessible(true);
            var recovery = (PullRecovery) field.get(null);
            if (args.length > 0) {
                recorded(pico, recovery, args);
            }
            else {
                general(pico, recovery);
            }
            System.out.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            error.printStackTrace();
            System.exit(1);
        }
    }

    private static void feed(PullRecovery recovery, Map<String, ?> frame) {
        String raw = mapper.writeValueAsString(frame);
        recovery.feed(raw, mapper.readTree(raw));
    }

    private static void general(MutablePicoContainer pico, PullRecovery recovery) throws Exception {
        var master = pico.getComponent(EventMaster.class);
        var dist = pico.getComponent(EventDistributor.class);
        var zones = new AtomicInteger();
        var timers = new CopyOnWriteArrayList<Instant>();
        dist.registerHandler(ZoneChangeEvent.class, (c, e) -> zones.incrementAndGet());
        dist.registerHandler(Timer.class, (c, e) -> {
            timers.add(recovery.clock.now());
            if (timers.size() == 1) {
                c.enqueue(new Timer(1000));
            }
        });
        var start = Instant.now().minusSeconds(10);
        recovery.begin(start.toString());
        feed(recovery, Map.of("type", "ChangeZone", "zoneID", 1, "zoneName", "Test"));
        feed(recovery, Map.of("type", "ChangePrimaryPlayer", "charID", 0x10000001, "charName", "Player"));
        feed(recovery, Map.of("rseq", "allCombatants", "combatants", List.of(
                Map.of("ID", 0x10000001, "Name", "Player", "Type", 1, "Job", 21, "PosX", 100, "PosY", 100))));
        var state = pico.getComponent(XivState.class);
        check(state.getPlayer() != null, "Snapshot did not populate player");
        check(state.getPlayer().getPos().x() == 100, "Snapshot did not populate position");
        master.pushEventAndWait(new BuffApplied(new XivStatusEffect(123), 30, state.getPlayer(), state.getPlayer(), 0));
        feed(recovery, Map.of("type", "ChangeZone", "zoneID", 1, "zoneName", "Test"));
        check(zones.get() == 1, "Repeated zone reset the pull");
        check(pico.getComponent(StatusEffectRepository.class).getBuffs().size() == 1, "Repeated zone cleared buffs");
        master.pushEvent(new Timer(1000));
        feed(recovery, Map.of("type", "LogLine", "rawLine", "00|" + start.plusSeconds(3) + "|0038|Player|Tick|0"));
        check(timers.size() == 2, "Replay did not advance delayed events");
        check(timers.get(0).equals(start.plusSeconds(1)) && timers.get(1).equals(start.plusSeconds(2)),
                "Replay timers ran at the wrong time: " + timers);
        recovery.end();
        master.pushEvent(new Timer(100));
        Thread.sleep(180);
        master.pushEventAndWait(new Tick());
        check(timers.size() == 3, "Live timer did not resume");
        var sequenceCalls = new AtomicInteger();
        var sequence = new SequentialTrigger<BaseEvent>(10000, BaseEvent.class,
                e -> e instanceof Seed, (e, s) -> {
                    s.waitMs(1000);
                    s.waitMs(500);
                    s.waitEvent(EchoEvent.class, echo -> echo.getLine().equals("Target"));
                    sequenceCalls.incrementAndGet();
                });
        dist.registerHandler(BaseEvent.class, sequence::feed);
        for (boolean live : List.of(true, false)) {
            start = Instant.now();
            recovery.begin(start.toString());
            master.pushEventAndWait(new Seed());
            if (live) {
                recovery.end();
            }
            feed(recovery, Map.of("type", "LogLine", "rawLine", "00|" + start.plusSeconds(2) + "|0038|Player|Target|0"));
            check(sequenceCalls.get() == (live ? 1 : 2), "Chained waits missed the buffered event after live handoff");
        }
        verifyHistory(pico, recovery);
        verifyAutomarks(pico, recovery);
        master.pushEventAndWait(new RefreshSpecificCombatantsRequest(List.of(0x10000001L)));
        System.out.println("VERIFIED snapshots duplicate zones buffs chained waits replay timers live timers automarks refresh requests");
    }

    private static void verifyHistory(MutablePicoContainer pico, PullRecovery recovery) throws Exception {
        var folder = Files.createTempDirectory("recovery-history-test");
        var file = folder.resolve("Network_test.log");
        Instant start = Instant.now().minusSeconds(10);
        String anchor = "00|" + start.plusSeconds(3) + "|0038|Player|Anchor|0";
        var seen = new ArrayList<String>();
        pico.getComponent(EventDistributor.class).registerHandler(EchoEvent.class, (c, e) -> seen.add(e.getLine()));
        try {
            Files.write(file, List.of(
                    "01|" + start + "|553|Raid|0",
                    "03|" + start + "|10000001|Player|15|64|0|0|World|0|0|10000|10000|10000|10000|0|0|100|100|0|0|0",
                    "00|invalid|0038|Player|Damaged line|0",
                    "20|" + start.plusSeconds(1) + "|40000001|Boss|NOT_HEX|Bad cast|10000001|Player|3|100|100|0|0|0",
                    "00|" + start.plusSeconds(2) + "|0038|Player|Later valid event|0", anchor));
            var restored = recovery.restore(folder, anchor, 0x553, 0x10000001, List.of("invalid JSON"), start);
            check(restored.skipped() == 3, "Malformed history fields and snapshot were not reported");
            check(seen.equals(List.of("Later valid event")), "Valid history after a malformed record was not processed");
        }
        finally {
            Files.deleteIfExists(file);
            Files.delete(folder);
        }
    }

    private static void verifyAutomarks(MutablePicoContainer pico, PullRecovery recovery) throws Exception {
        var received = new CopyOnWriteArrayList<String>();
        var server = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
            received.add(body);
            byte[] response = mapper.writeValueAsString(Map.of("id", mapper.readTree(body).path("id").asInt(),
                    "response", List.of())).getBytes(java.nio.charset.StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            var command = TriggeventCore.class.getDeclaredMethod("handleCommand", tools.jackson.databind.JsonNode.class);
            command.setAccessible(true);
            recovery.begin(Instant.now().minusSeconds(30).toString());
            command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "set_automark", "enable", true,
                    "uri", "http://127.0.0.1:" + server.getAddress().getPort() + "/")));
            pico.getComponent(EventDistributor.class).registerHandler(EchoEvent.class, (c, e) -> {
                if (e.getLine().startsWith("Mark")) {
                    c.accept(new AutoMarkSlotRequest(e.getLine().endsWith("2") ? 2 : 1));
                }
            });
            feed(recovery, Map.of("type", "LogLine", "rawLine",
                    "00|" + Instant.now().minusSeconds(20) + "|0038|Player|Mark|0"));
            command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "recover_end")));
            feed(recovery, Map.of("type", "LogLine", "rawLine",
                    "00|" + Instant.now().minusSeconds(10) + "|0038|Player|Mark|0"));
            Thread.sleep(500);
            check(received.stream().noneMatch(body -> body.contains("ExecuteCommand")), "Historical automark escaped catch-up");
            feed(recovery, Map.of("type", "LogLine", "rawLine",
                    "00|" + Instant.now() + "|0038|Player|Mark|0"));
            for (int retry = 0; retry < 50 && received.stream().noneMatch(body -> body.contains("ExecuteCommand")); retry++) {
                Thread.sleep(100);
            }
            check(received.stream().filter(body -> body.contains("ExecuteCommand")).count() == 1, "Current automark was not delivered");
            var telesto = pico.getComponent(TelestoMain.class);
            setDelay(telesto.getCommandDelayBase(), 3500);
            setDelay(telesto.getCommandDelayPlus(), 0);
            for (int mark = 0; mark < 2; mark++) {
                feed(recovery, Map.of("type", "LogLine", "rawLine",
                        "00|" + Instant.now() + "|0038|Player|Mark|0"));
            }
            for (int retry = 0; retry < 100 && markCount(received) < 3; retry++) {
                Thread.sleep(100);
            }
            check(markCount(received) == 3, "Configured delays or a queued burst discarded current automarks");

            setDelay(telesto.getCommandDelayBase(), 500);
            for (String boundary : List.of("pause", "disable", "wipe")) {
                received.clear();
                feed(recovery, Map.of("type", "LogLine", "rawLine",
                        "00|" + Instant.now() + "|0038|Player|Mark|0"));
                if (boundary.equals("pause")) {
                    command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "pause_feed")));
                    command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "recover_end")));
                }
                else if (boundary.equals("disable")) {
                    command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "set_automark", "enable", false)));
                    command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "set_automark", "enable", true)));
                }
                else {
                    pico.getComponent(EventMaster.class).pushEventAndWait(new WipeEvent());
                }
                feed(recovery, Map.of("type", "LogLine", "rawLine",
                        "00|" + Instant.now() + "|0038|Player|Mark 2|0"));
                for (int retry = 0; retry < 30 && markCount(received) == 0; retry++) {
                    Thread.sleep(100);
                }
                Thread.sleep(600);
                var commands = received.stream().filter(body -> body.contains("ExecuteCommand")).toList();
                check(commands.size() == 1 && commands.get(0).contains("<2>"),
                        "Pending automark survived " + boundary + ": " + commands);
            }
            command.invoke(null, mapper.valueToTree(Map.of("nyaa_cmd", "set_automark", "enable", false)));
        }
        finally {
            server.stop(0);
        }
    }

    private static long markCount(List<String> received) {
        return received.stream().filter(body -> body.contains("ExecuteCommand")).count();
    }

    private static void setDelay(IntSetting setting, int delay) {
        try {
            setting.set(delay);
        }
        catch (RuntimeException ignored) {
            // The verification engine keeps settings in memory.
        }
        check(setting.get() == delay, "Command delay did not change");
    }

    private static void recorded(MutablePicoContainer pico, PullRecovery recovery, String[] args) throws Exception {
        Path path = Path.of(args[0]);
        int cut = Integer.parseInt(args[1]);
        var lines = Files.readAllLines(path).stream().filter(line -> !line.isBlank()).toList();
        if (args.length > 2 && !Boolean.getBoolean("recovery.reference")) {
            var history = new PullHistoryReader().read(Path.of(args[2]), lines.get(cut),
                    Long.decode(args[3]), Long.decode(args[4]));
            check(history.reason().isEmpty(), "History was not restored: " + history.reason());
            var joined = new ArrayList<>(history.lines());
            joined.addAll(lines.subList(cut, lines.size()));
            cut = history.lines().size();
            lines = joined;
            System.out.println("HISTORY_RESTORED " + cut);
        }
        var dist = pico.getComponent(EventDistributor.class);
        var calls = new ArrayList<String>();
        var trace = new ArrayList<Map<String, Object>>();
        var failures = new ArrayList<String>();
        dist.registerHandler(RawModifiedCallout.class, (c, e) -> calls.add(e.getDescription()));
        dist.registerHandler(SequentialTriggerFailedEvent.class, (c, e) -> failures.add(e.toString()));
        Instant boundary = ZonedDateTime.parse(lines.get(cut).split("\\|")[1]).toInstant();
        // Keep the synthetic live boundary ahead of the test process timeout so
        // slow history loading cannot make the first live callouts stale.
        Duration shift = Duration.between(boundary, Instant.now().plusSeconds(120));
        System.out.println("TIME_SHIFT " + shift.toMillis());
        var calloutId = TriggeventCore.class.getDeclaredMethod("calloutId", CalloutEvent.class);
        calloutId.setAccessible(true);
        dist.registerHandler(CalloutEvent.class, (c, e) -> {
            String id;
            try {
                id = (String) calloutId.invoke(null, e);
            }
            catch (ReflectiveOperationException error) {
                throw new RuntimeException(error);
            }
            trace.add(Map.of(
                "id", id == null ? "" : id,
                "tts", e.getCallText() == null ? "" : e.getCallText(),
                "text", e.getVisualText() == null ? "" : e.getVisualText(),
                "at", e.getEffectiveHappenedAt().minus(shift).toEpochMilli()));
        });
        Instant first = ZonedDateTime.parse(lines.get(0).split("\\|")[1]).toInstant().plus(shift);
        recovery.begin(first.toString());
        for (int index = 0; index < lines.size(); index++) {
            if (index == cut) {
                calls.clear();
                trace.clear();
                if (!Boolean.getBoolean("recovery.reference")) {
                    recovery.end();
                }
                System.out.println("RECOVERY_BOUNDARY");
            }
            String[] parts = lines.get(index).split("\\|", 3);
            String raw = parts[0] + "|" + ZonedDateTime.parse(parts[1]).toInstant().plus(shift) + "|" + parts[2];
            feed(recovery, Map.of("type", "LogLine", "rawLine", raw));
        }
        check(failures.isEmpty(), "Chain failures: " + failures);
        for (String call : calls) {
            System.out.println("RECOVERED_CALL " + call);
        }
        for (var call : trace) {
            System.out.println("CALL_TRACE " + mapper.writeValueAsString(call));
        }
        check(!calls.isEmpty(), "No calls after the recovery boundary");
    }
}
