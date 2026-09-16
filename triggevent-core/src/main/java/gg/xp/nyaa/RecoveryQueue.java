package gg.xp.nyaa;

import gg.xp.reevent.events.BasicEventQueue;
import gg.xp.reevent.events.Event;
import gg.xp.xivsupport.events.delaytest.BaseDelayedEvent;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.Comparator;
import java.util.PriorityQueue;

/** Runs pending timers on the same clock as the recovered log. */
final class RecoveryQueue extends BasicEventQueue {
    private record Timer(Instant due, long order, Event event) {}
    private final RecoveryClock clock;
    private final ArrayDeque<Event> ready = new ArrayDeque<>();
    private final PriorityQueue<Timer> timers = new PriorityQueue<>(
            Comparator.comparing(Timer::due).thenComparingLong(Timer::order));
    private long order;

    RecoveryQueue(RecoveryClock clock) {
        this.clock = clock;
    }

    @Override
    public synchronized void push(Event event) {
        long at = event.delayedEnqueueAt();
        if (at > 0) {
            long basis = event instanceof BaseDelayedEvent delayed
                    ? delayed.getTimeBasis() : System.currentTimeMillis();
            timers.add(new Timer(clock.now().plusMillis(Math.max(0, at - basis)), order++, event));
        }
        else {
            ready.add(event);
        }
        notifyAll();
    }

    synchronized Instant nextTimer() {
        return timers.isEmpty() ? null : timers.peek().due();
    }

    synchronized void advance(Instant time) {
        clock.advance(time);
        releaseTimers();
        notifyAll();
    }

    private void releaseTimers() {
        while (!timers.isEmpty() && !timers.peek().due().isAfter(clock.now())) {
            ready.add(timers.remove().event());
        }
    }

    @Override
    public synchronized Event pull() {
        while (true) {
            releaseTimers();
            if (!ready.isEmpty()) {
                Event event = ready.remove();
                notifyAll();
                return event;
            }
            long wait = 1000;
            if (!clock.replaying() && !timers.isEmpty()) {
                wait = Math.max(1, Duration.between(clock.now(), timers.peek().due()).toMillis());
            }
            try {
                wait(wait);
            }
            catch (InterruptedException e) {
                throw new RuntimeException(e);
            }
        }
    }

    @Override
    public synchronized int pendingSize() {
        return ready.size();
    }

    @Override
    public synchronized void waitDrain() {
        while (!ready.isEmpty()) {
            try {
                wait(1000);
            }
            catch (InterruptedException e) {
                throw new RuntimeException(e);
            }
        }
    }
}
