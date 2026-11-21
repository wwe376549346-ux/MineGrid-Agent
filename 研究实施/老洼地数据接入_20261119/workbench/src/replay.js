export const LAST_HOUR = 23;
export const STEP_MS = 700;
export const hourOf = event => Number(event.time.slice(11, 13));
export const clock = hour => String(hour).padStart(2, "0") + ":00";

export function begin(events) {
  return {events, hour: 0, playing: false, notified: new Set(), alert: null};
}

function move(state, hour, playing) {
  const next = state.events.filter(event => hourOf(event) <= hour && !state.notified.has(event.id));
  const notified = next.length ? new Set([...state.notified, ...next.map(event => event.id)]) : state.notified;
  return {
    ...state, hour, notified, playing: playing && hour < LAST_HOUR,
    alert: next.length ? {events: next} : state.alert,
  };
}

export function replay(state, action) {
  switch (action.type) {
    case "play": {
      const initial = state.hour === LAST_HOUR ? {...state, alert: null} : state;
      return move(initial, state.hour === LAST_HOUR ? 0 : state.hour, true);
    }
    case "tick":
      return state.playing ? move(state, Math.min(state.hour + 1, LAST_HOUR), true) : state;
    case "step":
      return move(state, Math.min(state.hour + 1, LAST_HOUR), false);
    case "pause":
      return {...state, playing: false};
    case "seek":
      return move({...state, alert: null}, action.hour, false);
    case "reset":
      return {...state, hour: 0, playing: false, alert: null};
    case "dismiss":
      return {...state, alert: null};
    default:
      return state;
  }
}

export function visibleEvents(state) {
  return state.events.filter(event => hourOf(event) <= state.hour && state.notified.has(event.id));
}
