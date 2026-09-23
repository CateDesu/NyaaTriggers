package gg.xp.nyaa;

import gg.xp.reevent.events.BaseEvent;
import gg.xp.reevent.events.EventContext;
import gg.xp.reevent.events.TypedEventHandler;
import gg.xp.xivsupport.events.actlines.events.*;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.DutyCommenceEvent;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.FadeOutEvent;
import gg.xp.xivsupport.events.actlines.events.actorcontrol.VictoryEvent;
import gg.xp.xivsupport.events.misc.pulls.PullEndedEvent;
import gg.xp.xivsupport.events.misc.pulls.PullStartedEvent;
import gg.xp.xivsupport.events.state.XivState;
import gg.xp.xivsupport.events.state.combatstate.StatusEffectRepository;
import gg.xp.xivsupport.events.triggers.seq.SequentialTrigger;
import gg.xp.xivsupport.models.XivCombatant;
import gg.xp.xivsupport.speech.BasicCalloutEvent;
import tools.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class CustomTriggers implements TypedEventHandler<BaseEvent> {
    private static final Set<String> EVENTS = Set.of("cast", "ability", "status_gain", "status_loss", "headmarker", "tether");
    private static final Set<String> TARGETS = Set.of("any", "me", "start_target", "start_source");
    private static final Set<String> CONDITIONS = Set.of("always", "start_id", "event_id", "my_status", "no_my_status");
    private static final Set<String> TOKENS = Set.of("source", "target", "start.source", "start.target", "id", "start.id", "player");
    private static final Pattern PLACEHOLDER = Pattern.compile("\\{([^{}]*)}");
    private static final Pattern EDGE_WHITESPACE = Pattern.compile(
            "^[\\s\\x1c-\\x1f]+|[\\s\\x1c-\\x1f]+$", Pattern.UNICODE_CHARACTER_CLASS);
    private final XivState state;
    private final StatusEffectRepository buffs;
    private Map<String, Rule> rules = new LinkedHashMap<>();
    private Long zone;

    static final class Configure extends BaseEvent {
        final JsonNode definitions;
        String error;

        Configure(JsonNode definitions) {
            this.definitions = definitions;
        }
    }

    static final class Callout extends BasicCalloutEvent {
        final String id;

        Callout(String id, String text) {
            super(text, text, 5000);
            this.id = id;
        }
    }

    private record Match(String event, Set<Long> ids, String target) {
        boolean matches(BaseEvent event, BaseEvent start, XivCombatant player) {
            if (!this.event.equals(eventType(event)) || !ids.contains(eventId(event))) {
                return false;
            }
            XivCombatant targetActor = CustomTriggers.target(event);
            return switch (target) {
                case "any" -> !(event instanceof AbilityUsedEvent ability) || ability.isFirstTarget();
                case "me" -> same(targetActor, player);
                case "start_target" -> same(targetActor, CustomTriggers.target(start));
                case "start_source" -> same(targetActor, source(start));
                default -> false;
            };
        }
    }

    private record Step(String kind, Match match, long delay, String condition,
                        Set<Long> ids, String text, String otherwise) {}

    private final class Rule {
        final JsonNode definition;
        final SequentialTrigger<BaseEvent> sequence;

        Rule(JsonNode data) {
            definition = data.deepCopy();
            String id = data.path("id").asText();
            require(id.matches("nyaa:[0-9a-f-]{36}"), "Invalid callout ID");
            String name = text(data, "name", 200);
            require(!strip(name).isEmpty(), "Enter a callout name");
            text(data, "fight", 200);
            long zoneId = integer(data, "zone_id", 0, 0xFFFFFFFFL);
            int timeout = (int) (number(data, "timeout_s", 1, 600) * 1000);
            Match startMatch = match(data.path("start"), true);
            JsonNode rawSteps = data.path("steps");
            require(rawSteps.isArray() && !rawSteps.isEmpty() && rawSteps.size() <= 32, "Invalid callout steps");
            List<Step> steps = new ArrayList<>();
            boolean hasCallout = false;
            long delays = 0;
            for (JsonNode step : rawSteps) {
                String kind = step.path("kind").asText();
                switch (kind) {
                    case "wait" -> steps.add(new Step(kind, match(step, false), 0, "", Set.of(), "", ""));
                    case "delay" -> {
                        long delay = (long) (number(step, "seconds", 0.1, 600) * 1000);
                        delays += delay;
                        steps.add(new Step(kind, null, delay, "", Set.of(), "", ""));
                    }
                    case "callout" -> {
                        hasCallout = true;
                        String condition = step.path("condition").asText();
                        require(CONDITIONS.contains(condition), "Invalid callout condition");
                        if (step.has("ids")) text(step, "ids", 512);
                        Set<Long> ids = condition.equals("always") ? Set.of() : ids(step);
                        String text = template(step, "text");
                        String otherwise = template(step, "otherwise");
                        require(!strip(text).isEmpty() || !condition.equals("always") && !strip(otherwise).isEmpty(), "Enter spoken text");
                        steps.add(new Step(kind, null, 0, condition, ids, text, otherwise));
                    }
                    default -> throw new IllegalArgumentException("Invalid step type");
                }
            }
            require(hasCallout && delays < timeout, "Check callouts and total timeout");
            sequence = new SequentialTrigger<>(timeout, BaseEvent.class,
                    e -> (zoneId == 0 || state.getZone() != null && state.getZone().getId() == zoneId)
                            && startMatch.matches(e, null, state.getPlayer()),
                    (first, controller) -> {
                        BaseEvent current = first;
                        for (Step step : steps) {
                            switch (step.kind) {
                                case "wait" -> current = controller.waitEvent(BaseEvent.class,
                                        e -> step.match.matches(e, first, state.getPlayer()));
                                case "delay" -> controller.waitMs(step.delay);
                                case "callout" -> {
                                    boolean yes = switch (step.condition) {
                                        case "start_id" -> step.ids.contains(eventId(first));
                                        case "event_id" -> step.ids.contains(eventId(current));
                                        case "my_status" -> hasStatus(step.ids);
                                        case "no_my_status" -> state.getPlayer() != null && !hasStatus(step.ids);
                                        default -> true;
                                    };
                                    String spoken = expand(yes ? step.text : step.otherwise, first, current);
                                    if (!strip(spoken).isEmpty()) {
                                        controller.accept(new Callout(id, spoken));
                                    }
                                }
                            }
                        }
                    });
            sequence.setHandlerName(name);
        }
    }

    CustomTriggers(XivState state, StatusEffectRepository buffs) {
        this.state = state;
        this.buffs = buffs;
    }

    @Override
    public Class<BaseEvent> getType() {
        return BaseEvent.class;
    }

    @Override
    public void handle(EventContext context, BaseEvent event) {
        if (event instanceof Configure config) {
            try {
                replace(config.definitions);
            }
            catch (RuntimeException error) {
                config.error = error.getMessage();
            }
            return;
        }
        if (event instanceof ZoneChangeEvent changed) {
            long next = changed.getZone().getId();
            if (zone == null || zone != next) {
                reset();
                zone = next;
            }
            return;
        }
        if (event instanceof WipeEvent || event instanceof FadeOutEvent || event instanceof VictoryEvent
                || event instanceof PullEndedEvent || event instanceof PullStartedEvent || event instanceof DutyCommenceEvent) {
            reset();
            return;
        }
        for (Rule rule : rules.values()) {
            rule.sequence.feed(context, event);
        }
    }

    private void replace(JsonNode definitions) {
        require(definitions.isArray() && definitions.size() <= 200, "Invalid Triggevent callouts");
        Map<String, Rule> next = new LinkedHashMap<>();
        for (JsonNode definition : definitions) {
            String id = definition.path("id").asText();
            require(!next.containsKey(id), "Duplicate callout ID");
            Rule old = rules.get(id);
            next.put(id, old != null && old.definition.equals(definition) ? old : new Rule(definition));
        }
        rules.forEach((id, rule) -> {
            if (next.get(id) != rule) {
                rule.sequence.stopSilently();
            }
        });
        rules = next;
    }

    private void reset() {
        rules.values().forEach(rule -> rule.sequence.stopSilently());
    }

    private boolean hasStatus(Set<Long> ids) {
        return state.getPlayer() != null && buffs.statusesOnTarget(state.getPlayer()).stream()
                .anyMatch(buff -> ids.contains(buff.getBuff().getId()));
    }

    private String expand(String text, BaseEvent first, BaseEvent current) {
        Map<String, String> values = Map.of(
                "source", name(source(current)), "target", name(target(current)),
                "start.source", name(source(first)), "start.target", name(target(first)),
                "id", Long.toHexString(eventId(current)).toUpperCase(),
                "start.id", Long.toHexString(eventId(first)).toUpperCase(), "player", name(state.getPlayer()));
        return PLACEHOLDER.matcher(text).replaceAll(m -> Matcher.quoteReplacement(values.get(m.group(1))));
    }

    private static String name(XivCombatant actor) {
        return actor == null ? "" : actor.getName();
    }

    private static boolean same(XivCombatant one, XivCombatant two) {
        return one != null && two != null && one.getId() == two.getId();
    }

    private static XivCombatant source(BaseEvent event) {
        return event instanceof HasSourceEntity e ? e.getSource() : null;
    }

    private static XivCombatant target(BaseEvent event) {
        return event instanceof HasTargetEntity e ? e.getTarget() : null;
    }

    private static String eventType(BaseEvent event) {
        if (event instanceof AbilityCastStart) return "cast";
        if (event instanceof AbilityUsedEvent) return "ability";
        if (event instanceof BuffApplied) return "status_gain";
        if (event instanceof BuffRemoved) return "status_loss";
        if (event instanceof HeadMarkerEvent) return "headmarker";
        if (event instanceof TetherEvent) return "tether";
        return "";
    }

    private static long eventId(BaseEvent event) {
        if (event instanceof HasAbility e) return e.getAbility().getId();
        if (event instanceof HasStatusEffect e) return e.getBuff().getId();
        if (event instanceof HeadMarkerEvent e) return e.getMarkerId();
        if (event instanceof TetherEvent e) return e.getId();
        return -1;
    }

    private static Match match(JsonNode data, boolean start) {
        String event = data.path("event").asText();
        String target = data.path("target").asText();
        require(EVENTS.contains(event) && TARGETS.contains(target), "Invalid event or target");
        require(!start || target.equals("any") || target.equals("me"), "Invalid starting target");
        return new Match(event, ids(data), target);
    }

    private static Set<Long> ids(JsonNode data) {
        String text = text(data, "ids", 512);
        Set<Long> ids = new HashSet<>();
        for (String part : text.split("\\|", -1)) {
            String id = strip(part);
            require(id.matches("(?:0[xX])?[0-9a-fA-F]{1,8}"), "Enter hexadecimal IDs separated by |");
            ids.add(Long.parseLong(id.replaceFirst("^0[xX]", ""), 16));
        }
        return Set.copyOf(ids);
    }

    private static String text(JsonNode data, String key, int max) {
        JsonNode value = data.path(key);
        require(value.isString() && value.asText().length() <= max, "Invalid " + key);
        return value.asText();
    }

    private static String strip(String text) {
        return EDGE_WHITESPACE.matcher(text).replaceAll("");
    }

    private static String template(JsonNode data, String key) {
        String text = text(data, key, 2000);
        Matcher matcher = PLACEHOLDER.matcher(text);
        while (matcher.find()) require(TOKENS.contains(matcher.group(1)), "Unknown callout placeholder");
        String rest = matcher.replaceAll("");
        require(!rest.contains("{") && !rest.contains("}"), "Invalid callout placeholder");
        return text;
    }

    private static long integer(JsonNode data, String key, long min, long max) {
        require(data.path(key).isIntegralNumber(), "Invalid " + key);
        return (long) number(data, key, min, max);
    }

    private static double number(JsonNode data, String key, double min, double max) {
        JsonNode value = data.path(key);
        double number = value.asDouble(Double.NaN);
        require(value.isNumber() && Double.isFinite(number) && number >= min && number <= max, "Invalid " + key);
        return number;
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new IllegalArgumentException(message);
    }
}
