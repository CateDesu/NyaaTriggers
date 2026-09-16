package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.callouts.RawModifiedCallout;
import gg.xp.xivsupport.events.actlines.events.BuffApplied;
import gg.xp.xivsupport.events.actlines.events.ZoneChangeEvent;
import gg.xp.xivsupport.events.delaytest.BaseDelayedEvent;
import gg.xp.xivsupport.events.state.RefreshSpecificCombatantsRequest;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.events.state.combatstate.StatusEffectRepository;
import gg.xp.xivsupport.events.triggers.seq.SequentialTriggerFailedEvent;
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
            var recovery = (FeedRecovery) field.get(null);
            if (args.length > 0) {
                recorded(pico, recovery, Path.of(args[0]), Integer.parseInt(args[1]));
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

    private static void feed(FeedRecovery recovery, Map<String, ?> frame) {
        String raw = mapper.writeValueAsString(frame);
        recovery.feed(raw, mapper.readTree(raw));
    }

    private static void general(MutablePicoContainer pico, FeedRecovery recovery) throws Exception {
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
        master.pushEventAndWait(new RefreshSpecificCombatantsRequest(List.of(0x10000001L)));
        System.out.println("VERIFIED snapshots duplicate zones buffs replay timers live timers refresh requests");
    }

    private static void recorded(MutablePicoContainer pico, FeedRecovery recovery, Path path, int cut) throws Exception {
        var lines = Files.readAllLines(path).stream().filter(line -> !line.isBlank()).toList();
        var dist = pico.getComponent(EventDistributor.class);
        var calls = new ArrayList<String>();
        var failures = new ArrayList<String>();
        dist.registerHandler(RawModifiedCallout.class, (c, e) -> calls.add(e.getDescription()));
        dist.registerHandler(SequentialTriggerFailedEvent.class, (c, e) -> failures.add(e.toString()));
        Instant boundary = ZonedDateTime.parse(lines.get(cut).split("\\|")[1]).toInstant();
        Duration shift = Duration.between(boundary, Instant.now());
        Instant first = ZonedDateTime.parse(lines.get(0).split("\\|")[1]).toInstant().plus(shift);
        recovery.begin(first.toString());
        for (int index = 0; index < lines.size(); index++) {
            if (index == cut) {
                calls.clear();
                recovery.end();
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
        check(!calls.isEmpty(), "No calls after the recovery boundary");
    }
}
