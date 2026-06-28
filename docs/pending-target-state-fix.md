# Pending target state fix

This is a manual patch for `custom_components/inim_alarm/alarm_control_panel.py`.

## 1. Add import

Under:

```python
import logging
```

add:

```python
import time
```

## 2. Add constant

Under:

```python
_LOGGER = logging.getLogger(__name__)
```

add:

```python
PENDING_TARGET_TIMEOUT_SECONDS = 20
```

## 3. Add fields in `InimAlarmControlPanel.__init__`

After:

```python
self._pending_state: AlarmControlPanelState | None = None
self._armed_mode: str = "home"
```

add:

```python
self._pending_target_state: AlarmControlPanelState | None = None
self._pending_target_until: float | None = None
```

## 4. Add methods inside `InimAlarmControlPanel`

Add these methods inside the class, for example after `_configured_scenario`:

```python
def _set_pending_target(self, state: AlarmControlPanelState) -> None:
    """Hold the requested target state briefly to ignore stale cloud refreshes."""
    self._pending_target_state = state
    self._pending_target_until = time.monotonic() + PENDING_TARGET_TIMEOUT_SECONDS


def _clear_pending_target(self) -> None:
    """Clear pending target state."""
    self._pending_target_state = None
    self._pending_target_until = None


def _pending_target_active(self) -> AlarmControlPanelState | None:
    """Return the pending target state if it is still valid."""
    if self._pending_target_state is None or self._pending_target_until is None:
        return None

    if time.monotonic() <= self._pending_target_until:
        return self._pending_target_state

    self._clear_pending_target()
    return None
```

## 5. Modify `InimAlarmControlPanel.alarm_state`

Inside `alarm_state`, after:

```python
device = self.coordinator.get_device(self._device_id)
if not device:
    return None
```

add:

```python
pending_target = self._pending_target_active()
if pending_target == AlarmControlPanelState.DISARMED:
    return AlarmControlPanelState.DISARMED
```

Then after the alarm check:

```python
for area in areas:
    if area.get("Alarm", False):
        return AlarmControlPanelState.TRIGGERED
```

add:

```python
if pending_target is not None:
    return pending_target
```

## 6. Replace command methods in `InimAlarmControlPanel`

Replace `async_alarm_disarm`, `async_alarm_arm_home`, and `async_alarm_arm_away` with:

```python
async def async_alarm_disarm(self, code: str | None = None) -> None:
    """Send disarm command."""
    self._set_pending_target(AlarmControlPanelState.DISARMED)
    self._armed_mode = "home"
    self.async_write_ha_state()

    if not await self._async_run_action("Disarming", CONF_DISARM_SCENARIO, arm=False):
        self._clear_pending_target()
        self.async_write_ha_state()
        return

    await self.coordinator.async_request_refresh()


async def async_alarm_arm_home(self, code: str | None = None) -> None:
    """Send arm home command."""
    self._pending_state = None
    self._armed_mode = "home"
    self._set_pending_target(AlarmControlPanelState.ARMED_HOME)
    self.async_write_ha_state()

    if not await self._async_run_action(
        "Arming HOME",
        CONF_ARM_HOME_SCENARIO,
        arm=True,
    ):
        self._clear_pending_target()
        self.async_write_ha_state()
        return

    await self.coordinator.async_request_refresh()


async def async_alarm_arm_away(self, code: str | None = None) -> None:
    """Send arm away command."""
    self._pending_state = None
    self._armed_mode = "away"
    self._set_pending_target(AlarmControlPanelState.ARMED_AWAY)
    self.async_write_ha_state()

    if not await self._async_run_action(
        "Arming AWAY",
        CONF_ARM_AWAY_SCENARIO,
        arm=True,
    ):
        self._clear_pending_target()
        self.async_write_ha_state()
        return

    await self.coordinator.async_request_refresh()
```

## 7. Restart Home Assistant

```bash
rm -rf /config/custom_components/inim_alarm/__pycache__
ha core restart
```

Expected behavior:

- `Sblocca -> Fuori casa` should remain `Fuori casa` for 20 seconds and ignore stale cloud refreshes.
- If the command really fails, after 20 seconds it will fall back to the real state and HomeKit will notify the correction.
