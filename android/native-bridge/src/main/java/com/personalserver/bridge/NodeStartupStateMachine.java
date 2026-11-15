package com.personalserver.bridge;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Explicit state machine for PersonalServer node lifecycle.
 * Primary states:
 * STOPPED -> STARTING -> WAITING_FOR_VPN -> STARTING_SERVICES -> VERIFYING_SERVICES -> ONLINE
 * Additional states:
 * DEGRADED, NEEDS_USER_ACTION, FAILED, STOPPING
 */
public class NodeStartupStateMachine {

    public enum State {
        STOPPED,
        STARTING,
        WAITING_FOR_VPN,
        STARTING_SERVICES,
        VERIFYING_SERVICES,
        ONLINE,
        DEGRADED,
        NEEDS_USER_ACTION,
        FAILED,
        STOPPING
    }

    // Stage alias mapping for granular step logging & backward compatibility
    public enum Stage {
        STOPPED,
        STARTING,
        WAITING_FOR_VPN,
        STARTING_SERVICES,
        VERIFYING_SERVICES,
        ONLINE,
        DEGRADED,
        NEEDS_USER_ACTION,
        FAILED,
        STOPPING,
        APP_INITIALIZING,
        NATIVE_BRIDGE_STARTING,
        NATIVE_BRIDGE_READY,
        TERMUX_DISCOVERING,
        TERMUX_LAUNCHING,
        TERMUX_COMMAND_READY,
        TAILSCALE_CHECKING,
        TAILSCALE_CONNECTING,
        TAILSCALE_CONNECTED,
        WAITING_FOR_USER,
        PERSONALSERVER_STARTING,
        NODE_API_CHECKING,
        WORKER_CHECKING,
        CONTROLLER_CHECKING,
        HEALTH_CHECKING
    }

    public static class StepRecord {
        private final String name;
        private final long timestamp;
        private final long durationMs;
        private final String outcome; // SUCCESS, FAILED, SKIPPED, DISPATCHED, DEGRADED
        private final String details;

        public StepRecord(String name, String outcome, String details, long durationMs) {
            this.name = name;
            this.timestamp = System.currentTimeMillis();
            this.durationMs = durationMs;
            this.outcome = outcome;
            this.details = details;
        }

        public String getName() {
            return name;
        }

        public long getTimestamp() {
            return timestamp;
        }

        public long getDurationMs() {
            return durationMs;
        }

        public String getOutcome() {
            return outcome;
        }

        public String getDetails() {
            return details;
        }

        public JSONObject toJson() {
            JSONObject obj = new JSONObject();
            try {
                obj.put("name", name);
                obj.put("timestamp", timestamp);
                obj.put("duration_ms", durationMs);
                obj.put("outcome", outcome);
                obj.put("details", details != null ? details : JSONObject.NULL);
            } catch (JSONException ignored) {}
            return obj;
        }
    }

    public interface StateListener {
        void onStateChanged(State state, String message);
    }

    private volatile State currentState = State.STOPPED;
    private volatile String currentMessage = "Node is stopped";
    private final List<StepRecord> history = Collections.synchronizedList(new ArrayList<>());
    private volatile StateListener listener = null;
    private final Object lock = new Object();

    public NodeStartupStateMachine() {}

    public void setListener(StateListener listener) {
        this.listener = listener;
    }

    public State getCurrentState() {
        return currentState;
    }

    public Stage getCurrentStage() {
        try {
            return Stage.valueOf(currentState.name());
        } catch (Exception e) {
            return Stage.STOPPED;
        }
    }

    public String getCurrentMessage() {
        return currentMessage;
    }

    public void transitionTo(State newState, String message) {
        long now = System.currentTimeMillis();
        synchronized (lock) {
            currentState = newState;
            currentMessage = message;
            history.add(new StepRecord("STATE: " + newState.name(), "TRANSITION", message, 0));
        }
        notifyListener(newState, message);
    }

    public void logStep(String name, String outcome, String details, long durationMs) {
        synchronized (lock) {
            history.add(new StepRecord(name, outcome, details, durationMs));
        }
    }

    // Compatibility methods for Stage-based calls
    public void startStage(Stage stage, long timeoutMs, String message) {
        try {
            State mappedState = State.valueOf(stage.name());
            transitionTo(mappedState, message);
        } catch (IllegalArgumentException e) {
            logStep(stage.name(), "START", message, 0);
        }
    }

    public void completeStage(Stage stage, String result, String message) {
        logStep(stage.name(), result != null ? result : "SUCCESS", message, 0);
    }

    public void failStage(Stage stage, String failureReason) {
        logStep(stage.name(), "FAILED", failureReason, 0);
        transitionTo(State.FAILED, failureReason);
    }

    public void skipStage(Stage stage, String reason) {
        logStep(stage.name(), "SKIPPED", reason, 0);
    }

    public void reset(State initialState) {
        synchronized (lock) {
            currentState = initialState;
            currentMessage = "Reset to " + initialState.name();
            history.clear();
        }
        notifyListener(initialState, currentMessage);
    }

    public void reset(Stage initialStage) {
        try {
            reset(State.valueOf(initialStage.name()));
        } catch (Exception e) {
            reset(State.STOPPED);
        }
    }

    public List<StepRecord> getHistory() {
        synchronized (lock) {
            return new ArrayList<>(history);
        }
    }

    private void notifyListener(State state, String message) {
        StateListener l = this.listener;
        if (l != null) {
            try {
                l.onStateChanged(state, message);
            } catch (Exception ignored) {}
        }
    }

    public JSONObject toJson() {
        JSONObject obj = new JSONObject();
        try {
            obj.put("current_state", currentState.name());
            obj.put("current_message", currentMessage);
            JSONArray arr = new JSONArray();
            synchronized (lock) {
                for (StepRecord r : history) {
                    arr.put(r.toJson());
                }
            }
            obj.put("history", arr);
        } catch (JSONException ignored) {}
        return obj;
    }

    public String getFormattedSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("Current State: ").append(currentState.name()).append(" (").append(currentMessage).append(")\n\n");
        sb.append("Recent Execution Log:\n");
        synchronized (lock) {
            int start = Math.max(0, history.size() - 15);
            for (int i = start; i < history.size(); i++) {
                StepRecord r = history.get(i);
                sb.append(" • ").append(r.getName())
                        .append(": ").append(r.getOutcome());
                if (r.getDurationMs() > 0) {
                    sb.append(" (").append(r.getDurationMs()).append("ms)");
                }
                if (r.getDetails() != null && !r.getDetails().isEmpty()) {
                    sb.append(" [").append(r.getDetails()).append("]");
                }
                sb.append("\n");
            }
        }
        return sb.toString();
    }
}
