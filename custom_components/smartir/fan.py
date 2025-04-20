import asyncio
import aiofiles
import json
import logging
import os.path

import voluptuous as vol

from homeassistant.components.fan import (
    FanEntity, FanEntityFeature,
    PLATFORM_SCHEMA, DIRECTION_REVERSE, DIRECTION_FORWARD)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item
)
from . import COMPONENT_ABS_DIR, Helper
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Fan"
DEFAULT_DELAY = 1.5

CONF_UNIQUE_ID = 'unique_id'
CONF_DEVICE_CODE = 'device_code'
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = 'power_sensor'
CONF_POWER_SWITCH = 'power_switch'
CONF_DUPLICATING_FAN_SWITCHES = 'duplicating_fan_switches'
CONF_DUPLICATING_SWITCH_DELAY = "duplicating_switch_delay"
DEFAULT_DUPLICATING_SWITCH_DELAY = 1.5

SPEED_OFF = "off"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.string,
    vol.Optional(CONF_POWER_SWITCH): cv.entity_id,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
    vol.Optional(CONF_DUPLICATING_FAN_SWITCHES): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional(CONF_DUPLICATING_SWITCH_DELAY, default=DEFAULT_DUPLICATING_SWITCH_DELAY): cv.positive_float,
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Fan platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'fan')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    if not os.path.isdir(device_files_absdir):
        os.makedirs(device_files_absdir)

    device_json_filename = str(device_code) + '.json'
    device_json_path = os.path.join(device_files_absdir, device_json_filename)

    if not os.path.exists(device_json_path):
        _LOGGER.warning("Couldn't find the device Json file. The component will " \
                        "try to download it from the GitHub repo.")

        try:
            codes_source = ("https://raw.githubusercontent.com/"
                            "smartHomeHub/SmartIR/master/"
                            "codes/fan/{}.json")

            await Helper.downloader(codes_source.format(device_code), device_json_path)
        except Exception:
            _LOGGER.error("There was an error while downloading the device Json file. " \
                          "Please check your internet connection or if the device code " \
                          "exists on GitHub. If the problem still exists please " \
                          "place the file manually in the proper directory.")
            return

    try:
        async with aiofiles.open(device_json_path, mode='r') as j:
            _LOGGER.debug(f"loading json file {device_json_path}")
            content = await j.read()
            device_data = json.loads(content)
            _LOGGER.debug(f"{device_json_path} file loaded")
    except Exception:
        _LOGGER.error("The device JSON file is invalid")
        return

    async_add_entities([SmartIRFan(
        hass, config, device_data
    )])

class SmartIRFan(FanEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_switch = config.get(CONF_POWER_SWITCH)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._duplicating_fan_switches = config.get(CONF_DUPLICATING_FAN_SWITCHES, [])
        self._duplicating_switch_delay = config.get(
            CONF_DUPLICATING_SWITCH_DELAY, DEFAULT_DUPLICATING_SWITCH_DELAY
        )

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._speed_list = device_data['speed']
        self._commands = device_data['commands']
        self._power_mapping = device_data.get('powerMapping')
        
        self._speed = SPEED_OFF
        self._direction = None
        self._last_on_speed = None
        self._oscillating = None
        self._on_button = None
        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.TURN_ON)

        if (DIRECTION_REVERSE in self._commands and \
            DIRECTION_FORWARD in self._commands):
            self._direction = DIRECTION_REVERSE
            self._support_flags = (
                self._support_flags | FanEntityFeature.DIRECTION)
        if ('oscillate' in self._commands):
            self._oscillating = False
            self._support_flags = (
                self._support_flags | FanEntityFeature.OSCILLATE)
        if ('on' in self._commands):
            self._on_button = True


        self._temp_lock = asyncio.Lock()
        self._on_by_remote = False

        #Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller, 
            self._commands_encoding,
            self._controller_data,
            self._delay)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()
    
        last_state = await self.async_get_last_state()

        if last_state is not None:
            if 'speed' in last_state.attributes:
                self._speed = last_state.attributes['speed']

            #If _direction has a value the direction controls appears 
            #in UI even if SUPPORT_DIRECTION is not provided in the flags
            if ('direction' in last_state.attributes and \
                self._support_flags & FanEntityFeature.DIRECTION):
                self._direction = last_state.attributes['direction']

            if 'last_on_speed' in last_state.attributes:
                self._last_on_speed = last_state.attributes['last_on_speed']

            if self._power_sensor:
                async_track_state_change(
                    self.hass, self._power_sensor,
                    self._async_power_sensor_changed
                )


    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the display name of the fan."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        if (self._on_by_remote or \
            self._speed != SPEED_OFF):
            return STATE_ON
        return SPEED_OFF

    @property
    def percentage(self):
        """Return speed percentage of the fan."""
        if (self._speed == SPEED_OFF):
            return 0

        return ordered_list_item_to_percentage(self._speed_list, self._speed)

    @property
    def speed_count(self):
        """Return the number of speeds the fan supports."""
        return len(self._speed_list)

    @property
    def oscillating(self):
        """Return the oscillation state."""
        return self._oscillating

    @property
    def current_direction(self):
        """Return the direction state."""
        return self._direction

    @property
    def last_on_speed(self):
        """Return the last non-idle speed."""
        return self._last_on_speed

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            'last_on_speed': self._last_on_speed,
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding,
        }

    async def async_set_percentage(self, percentage: int):
        """Set the desired speed for the fan."""
        if (percentage == 0):
             self._speed = SPEED_OFF
        else:
            self._speed = percentage_to_ordered_list_item(
                self._speed_list, percentage)

        if not self._speed == SPEED_OFF:
            self._last_on_speed = self._speed

        await self.send_command()
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set oscillation of the fan."""
        self._oscillating = oscillating

        await self.send_command()
        self.async_write_ha_state()

    async def async_set_direction(self, direction: str):
        """Set the direction of the fan"""
        self._direction = direction

        if not self._speed.lower() == SPEED_OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_turn_on(self, percentage: int = None, preset_mode: str = None, **kwargs):
        """Turn on the fan."""
        if self._power_switch:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self._power_switch}, blocking=True
            )
        if percentage is None:
            percentage = ordered_list_item_to_percentage(
                self._speed_list, self._last_on_speed or self._speed_list[0])

        await self.async_set_percentage(percentage)

    async def async_turn_off(self):
        """Turn off the fan."""
        await self.async_set_percentage(0)
        if self._power_switch:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self._power_switch}, blocking=True
            )
    async def _delay_for_switches(self):
        """Sleep asynchronously for the amount of time described in the configuration if there are switches waiting to be removed"""
        if len(self._duplicating_switch_states) > 0:
            _LOGGER.warning("Before delay")
            # Wait before sending new command
            await asyncio.sleep(self._duplicating_switch_delay)
            _LOGGER.warning("After delay")
    async def _prepare_duplicating_switches(self):
        """Turn off duplicating switches and record their original states."""
        self._duplicating_switch_states = {}

        for switch in self._duplicating_fan_switches:
            try:
                state = self.hass.states.get(switch)
                if state and state.state == STATE_ON:
                    self._duplicating_switch_states[switch] = STATE_ON
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": switch}, blocking=True
                    )
                    _LOGGER.debug(f"Turned off duplicating switch: {switch}")
                else:
                    self._duplicating_switch_states[switch] = STATE_OFF
                    _LOGGER.debug(f"Duplicating switch already off: {switch}")
            except Exception as e:
                _LOGGER.warning(f"Failed to prepare duplicating switch {switch}: {e}")

        # Delay till next execution
        await self._delay_for_switches()

    async def _restore_duplicating_switches(self):
        """Turn on only the switches that were originally on."""
        # Delay before restoring switches
        await self._delay_for_switches()
        for switch, original_state in self._duplicating_switch_states.items():
            try:
                if original_state == STATE_ON:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": switch}, blocking=True
                    )
                    _LOGGER.debug(f"Restored duplicating switch ON: {switch}")
            except Exception as e:
                _LOGGER.warning(f"Failed to restore duplicating switch {switch}: {e}")

    async def send_command(self):
        async with self._temp_lock:
            self._on_by_remote = False
            speed = self._speed
            direction = self._direction or 'default'
            oscillating = self._oscillating

            # Turn off duplicating switches and wait
            await self._prepare_duplicating_switches()

            if speed.lower() == SPEED_OFF:
                command = self._commands['off']
            elif oscillating:
                command = self._commands['oscillate']
            else:
                if (self._on_button):
                    await self._controller.send(self._commands['on'])
                command = self._commands[direction][speed]

            try:
                await self._controller.send(command)
            except Exception as e:
                _LOGGER.exception(e)

            # Turn back on switches if they were originally on
            await self._restore_duplicating_switches()

    def power_to_speed(self, watts):
        try:
            watts = float(watts)
            _LOGGER.debug(f"{watts} read from sensor")
            if watts <= self._power_mapping['off']:
                _LOGGER.debug(f"Reading zero watts and setting to speed off")
                return SPEED_OFF
            for speed in self._speed_list:
                _LOGGER.debug(f"Finding Max Power for Speed {speed}")
                max_power = self._power_mapping.get(speed)
                _LOGGER.debug(f"Found Max Power - {max_power} for Speed {speed}")
                if max_power is not None and watts < max_power:
                    _LOGGER.debug(f"Returning Speed {speed}")
                    return speed
        except Exception as e:
            _LOGGER.exception(f"Error converting power to speed: {e}")
            return None


    async def _async_power_sensor_changed(self, entity_id, old_state, new_state):
        """Handle power sensor changes."""
        if new_state is None or new_state.state in (STATE_UNKNOWN, ""):
            return

        if new_state.state == old_state.state:
            return

        new_speed = self.power_to_speed(new_state.state)

        if new_speed is None or new_speed == self._speed:
            return

        _LOGGER.debug(f"Detected fan speed change via power usage: {new_speed}")
        self._speed = new_speed
        if new_speed != SPEED_OFF:
            self._last_on_speed = new_speed
            self._on_by_remote = True
        else:
            self._on_by_remote = False
        self.async_write_ha_state()
