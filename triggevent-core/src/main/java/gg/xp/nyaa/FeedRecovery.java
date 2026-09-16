package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.Event;
import gg.xp.reevent.events.EventContext;
import gg.xp.reevent.events.EventHandler;
import gg.xp.reevent.events.EventMaster;
import gg.xp.xivsupport.events.ACTLogLineEvent;
import gg.xp.xivsupport.events.ws.ActWsRawMsg;
import gg.xp.xivsupport.events.delaytest.BaseDelayedEvent;
import gg.xp.xivsupport.events.actlines.events.RawAddCombatantEvent;
import gg.xp.xivsupport.events.actlines.events.RawRemoveCombatantEvent;
import gg.xp.xivsupport.events.actlines.events.ZoneChangeEvent;
import gg.xp.xivsupport.events.state.XivStateImpl;
import gg.xp.xivsupport.models.XivZone;
import gg.xp.xivsupport.sys.KnownLogSource;
import gg.xp.xivsupport.sys.PrimaryLogSource;
import tools.jackson.databind.JsonNode;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.util.List;
import java.util.function.Supplier;

final class FeedRecovery implements EventHandler<Event> {
    private static final class Tick extends BaseEvent {}
    final RecoveryClock clock;
    private final RecoveryQueue queue;
    private final EventMaster master;
    private final PrimaryLogSource source;
    private final Supplier<XivStateImpl> state;
    private long zone = -1;

    FeedRecovery(RecoveryClock clock, RecoveryQueue queue, EventMaster master, PrimaryLogSource source, Supplier<XivStateImpl> state) {
        this.clock = clock;
        this.queue = queue;
        this.master = master;
        this.source = source;
        this.state = state;
    }

    void begin(String timestamp) {
        master.pushEventAndWait(new Tick());
        clock.begin(Instant.parse(timestamp));
        source.setLogSource(KnownLogSource.ACT_LOG_FILE);
    }

    void end() {
        master.pushEventAndWait(new Tick());
        clock.resume();
        queue.advance(clock.now());
        source.setLogSource(KnownLogSource.WEBSOCKET_LIVE);
    }

    private void advance(Instant time) {
        if (!clock.replaying()) {
            clock.follow(time);
            return;
        }
        Instant next;
        while ((next = queue.nextTimer()) != null && !next.isAfter(time)) {
            queue.advance(next);
            master.pushEventAndWait(new Tick());
        }
        queue.advance(time);
    }

    void feed(String raw, JsonNode frame) {
        String type = frame.path("type").asText("");
        long newZone = -1;
        String zoneName = "";
        if ("ChangeZone".equals(type)) {
            newZone = frame.path("zoneID").asLong(-1);
            zoneName = frame.path("zoneName").asText("");
        }
        if ("LogLine".equals(type)) {
            String line = frame.path("rawLine").asText("");
            String[] parts = line.split("\\|", 5);
            if (parts.length >= 3) {
                advance(ZonedDateTime.parse(parts[1]).toInstant());
                if ("01".equals(parts[0]) || "1".equals(parts[0])) {
                    newZone = Long.parseLong(parts[2], 16);
                    zoneName = parts.length > 3 ? parts[3] : "";
                }
            }
        }
        if (newZone >= 0) {
            if (newZone == zone) {
                return;
            }
            zone = newZone;
            master.pushEventAndWait(new ZoneChangeEvent(new XivZone(newZone, zoneName)));
            return;
        }
        master.pushEventAndWait(new ActWsRawMsg(raw));
    }

    @Override
    public int getOrder() {
        return -10000;
    }

    @Override
    public void handle(EventContext context, Event event) {
        if (!source.isActImport()) {
            if (event instanceof RawAddCombatantEvent added && added.getFullInfo() != null) {
                state.get().setSpecificCombatants(List.of(added.getFullInfo()));
            }
            else if (event instanceof RawRemoveCombatantEvent removed) {
                state.get().removeSpecificCombatant(removed.getEntity().getId());
            }
        }
        if (event instanceof BaseEvent base) {
            base.setTimeSource(clock);
            if (!(event instanceof ACTLogLineEvent)
                    && (event.getParent() == null || event instanceof BaseDelayedEvent)) {
                base.setHappenedAt(clock.now());
            }
        }
    }
}
