package gg.xp.nyaa;

import gg.xp.xivsupport.events.actlines.parsers.FakeACTTimeSource;
import java.time.Instant;

final class RecoveryClock extends FakeACTTimeSource {
    private Instant time = Instant.now();
    private long tick = System.nanoTime();
    private boolean replay;

    @Override
    public synchronized Instant now() {
        return replay ? time : time.plusNanos(System.nanoTime() - tick);
    }

    synchronized boolean replaying() {
        return replay;
    }

    synchronized void begin(Instant start) {
        time = start;
        replay = true;
    }

    synchronized void advance(Instant next) {
        if (next.isAfter(time)) {
            time = next;
        }
    }

    synchronized void resume() {
        tick = System.nanoTime();
        replay = false;
    }

    synchronized void follow(Instant next) {
        Instant current = now();
        time = next.isAfter(current) ? next : current;
        tick = System.nanoTime();
    }

    @Override
    public void setNewTime(Instant ignored) {
        // The feed owns the clock so individual parsers cannot rewind it.
    }
}
