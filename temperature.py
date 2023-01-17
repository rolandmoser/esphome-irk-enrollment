import math
import pytz
import numpy as np
import pandas as pd
import hassapi as hass
import adbase as ad
import datetime
import influx


def parse_conditional_expr(cause):
    """
    Copied from lights.py
    """
    present_state = 'on'
    absent_state = 'off'
    entity = cause
    if '==' in cause:
        xs = [x.strip() for x in cause.split('==')]
        entity = xs[0]
        present_state = xs[1]
        absent_state = None
    elif '!=' in cause:
        xs = [x.strip() for x in cause.split('!=')]
        entity = xs[0]
        present_state = None
        absent_state = xs[1]
    return present_state, absent_state, entity

def get_sensor_data(entity_id, column, start='-7d'):
    id_parts = entity_id.split('.')
    if len(id_parts) == 2:
        id_parts.append("value") # default for state field, otherwise attr
    q = f'''
    from(bucket: "homeassistant/autogen")
      |> range(start: {start})
      |> filter(fn: (r) => r.entity_id == "{id_parts[1]}" and r._field == "{id_parts[2]}" and r.domain == "{id_parts[0]}")
      |> pivot(rowKey:["_time"], columnKey: ["entity_id"], valueColumn: "_value")
      |> drop(columns: ["_measurement", "domain", "_start", "_stop"])
    '''
    df = influx.query_api.query_data_frame(q).drop(['result','table','_field'], axis=1)
    return df.rename(columns={id_parts[1]: column})

# TODO maybe this should be a linear model instead. Weather, outdoor temp, indoor temp, heating/cool mode. maybe cloudiness, indoor humidity?
class OffsetCalibration(hass.Hass):
    def initialize(self):
        self.thermostat_ent = self.args["climate_entity"]
        self.remote_temp_ent = self.args["temperature_entity"]
        runtime = datetime.time(0, 0, 0)
        self.run_in(self.compute_offsets, 0)
        self.run_hourly(self.compute_offsets, runtime)

    def compute_offsets(self, kwargs):
        for remote_temp_ent in self.remote_temp_ent:
            self.log(f"computing offset for sensor: {remote_temp_ent} from {self.thermostat_ent}")
            thermostat_ent_parts = self.thermostat_ent.split('.')
            remote_temp_ent_parts = remote_temp_ent.split('.')
            offset_entity = self.get_entity(f"sensor.offset_calibrated_{thermostat_ent_parts[1]}_{remote_temp_ent_parts[1]}")
            try:
                remote_temp = get_sensor_data(remote_temp_ent, column='remote_temp')
                temp = get_sensor_data(self.thermostat_ent + ".current_temperature", column='current_temp')
                target_temp = get_sensor_data(self.thermostat_ent + ".temperature", column='target_temp')
                mode = get_sensor_data(self.thermostat_ent + ".state", column='hvac_mode')
                base_times = pd.concat([x['_time'] for x in [temp, remote_temp, target_temp, mode]])
                base_times = base_times.sort_values().drop_duplicates()
                df = pd.merge_asof(base_times, temp, on='_time')
                df = pd.merge_asof(df, target_temp, on='_time')
                df = pd.merge_asof(df, mode, on='_time')
                df = pd.merge_asof(df, remote_temp, on='_time')
                offset_df = df.query("hvac_mode in ('heat', 'cool') and current_temp == target_temp").copy()
                offset_df._time= offset_df._time.dt.tz_convert('America/New_York')
                offset_df['delta'] = offset_df['remote_temp'] - offset_df['current_temp']
                result = offset_df[['hvac_mode', 'delta']].groupby('hvac_mode').describe()
                self.log(result)
                attrs = {
                    'heating_offset': 0,
                    'heating_stddev': 0,
                    'cooling_offset': 0,
                    'cooling_stddev': 0,
                }
                if 'heat' in result.index:
                    attrs = {
                        **attrs,
                        'heating_offset': result.loc['heat'].loc[('delta','mean')],
                        'heating_stddev': result.loc['heat'].loc[('delta','std')],
                    }
                if 'cool' in result.index:
                    attrs = {
                        **attrs,
                        'cooling_offset': result.loc['cool'].loc[('delta','mean')],
                        'cooling_stddev': result.loc['cool'].loc[('delta','std')],
                    }
                offset_entity.set_state(state='on', attributes = attrs)
            except Exception as e:
                self.error(e)
                offset_entity.set_state(state='error')

class ConvergenceSpeedCalibration(hass.Hass):
    def initialize(self):
        self.thermostat_ent = self.args["climate_entity"]
        runtime = datetime.time(0, 0, 0)
        self.run_in(self.compute_offsets, 0)
        self.run_hourly(self.compute_offsets, runtime)

    def compute_offsets(self, kwargs):
        temp = get_sensor_data(self.thermostat_ent + ".current_temperature", column="current_temp")
        target_temp = get_sensor_data(self.thermostat_ent + ".temperature", column="target_temp")
        mode = get_sensor_data(self.thermostat_ent + ".state", column="hvac_mode")
        base_times = pd.concat([x['_time'] for x in [temp, target_temp, mode]])
        base_times = base_times.sort_values().drop_duplicates()
        df = pd.merge_asof(base_times, temp, on='_time')
        df = pd.merge_asof(df, target_temp, on='_time')
        df = pd.merge_asof(df, mode, on='_time')
        df['_time'] = df['_time'].dt.tz_convert('America/New_York')
        df = df.set_index('_time')
        change_df = df[['hvac_mode', 'target_temp']].shift(1).rename(columns={'hvac_mode': 'mode_before', 'target_temp': 'target_before'})
        change_events_df = pd.merge(df, change_df, left_index=True, right_index=True).dropna(subset=['hvac_mode', 'mode_before'])
        change_events_df = change_events_df.query("(hvac_mode != mode_before or target_temp != target_before) and hvac_mode not in ['fan', 'off'] and current_temp != target_temp")

        stable_df = df.query('current_temp == target_temp').drop(['current_temp'],axis=1).rename(columns={'target_temp': 'stable_temp'})
        stable_df['stable_time'] = stable_df.index
        change_events_df['temp_delta'] = change_events_df['target_temp'] - change_events_df['current_temp']
        change_events_df = pd.merge_asof(change_events_df, stable_df, left_index=True, right_index=True, by='hvac_mode', direction='forward').dropna(subset=['stable_time'])
        change_events_df = change_events_df.query("(temp_delta > 0 and hvac_mode == 'heat') or (temp_delta < 0 and hvac_mode == 'cool')").copy()
        change_events_df['time_delta'] = (change_events_df['stable_time'] - change_events_df.index).dt.total_seconds()
        change_events_df['adapt_rate_degrees_per_hr'] = change_events_df['temp_delta'] / (change_events_df['time_delta'] / 3600.0)
        self.log(change_events_df)

class BasicThermostatController(hass.Hass):
    @ad.app_lock
    def initialize(self):
        self.thermostat = self.args["climate_entity"]
        self.max_diff_for_heat_pump = self.args["max_diff_for_heat_pump"]
        self.report_ent_name = self.args['report_entity']
        self.weather_ent = self.args.get("hourly_weather", "weather.home_hourly")
        runtime = datetime.time(0, 0, 0)
        self.listen_event(self.wind_down_event, self.args["events"]["sleep"]["name"], actionName= self.args["events"]["sleep"]["actionName"])
        self.listen_event(self.morning_alarm_event, self.args["events"]["wake"]["name"], actionName= self.args["events"]["wake"]["actionName"])
        self.run_daily(self.determine_if_warm_or_cool_day, '04:00:00')
        self.presence = [parse_conditional_expr(x) for x in self.args['presence']]
        self.people = {ent: 'unknown' for (_,_,ent) in self.presence}
        self.presence_state = 'home'
        if len(self.people) != len(self.presence):
            raise ValueError(f'Each tracked entity can only appear once: {self.presence}')
        for present_state, absent_state, entity in self.presence:
            if present_state:
                self.listen_state(self.did_arrive, entity, new=present_state, immediate=True)
            else:
                self.listen_state(self.did_arrive, entity, absent_state=absent_state, immediate=True)
            if absent_state:
                self.listen_state(self.did_leave, entity, new=absent_state, immediate=True)
            else:
                self.listen_state(self.did_leave, entity, present_state=present_state, immediate=True)
        self.determine_if_warm_or_cool_day({})

    @ad.app_lock
    def did_arrive(self, entity, attr, old, new, kwargs):
        if 'absent_state' in kwargs:
            if new == kwargs['absent_state']:
                return
        self.log(f"did arrive {entity} {attr} {old} {new} {kwargs}")
        self.people[entity] = 'home'
        self.update_temp_by_presence()

    @ad.app_lock
    def did_leave(self, entity, attr, old, new, kwargs):
        if 'present_state' in kwargs:
            if new == kwargs['present_state']:
                return
        self.log(f"did leave {entity} {attr} {old} {new} {kwargs}")
        self.people[entity] = 'away'
        self.update_temp_by_presence()

    def update_temp_by_presence(self):
        #self.log(f"people = {self.people}")
        if len(self.people) != len([k for k,v in self.people.items() if v != 'unknown']):
            #self.log("bailing early")
            return # don't do things before we know where people are
        any_home = False
        for ent, status in self.people.items():
            if status == 'home':
                any_home = True
        report_ent = self.get_entity(self.report_ent_name)
        self.log(f"updating {self.presence_state} {any_home} {self.people}")
        thermostat_state =  self.get_state(self.thermostat, attribute='all')
        if self.presence_state != 'home' and any_home:
            self.cancel_climb_heat_mode("presence change to home")
            self.presence_state = 'home'
            target_temp = self.today_conf['saved_temperature'] if 'saved_temperature' in self.today_conf else self.today_conf['target_temp']
            # if we are heating and the current temp is more than 4 degrees below the target, we must ramp to avoid using emheat mode
            current_temperature = thermostat_state['attributes']['current_temperature']
            if thermostat_state['state'] == 'heat' and current_temperature + self.max_diff_for_heat_pump < target_temp:
                # we are going to go into the climbing mode
                self.climb_target = target_temp
                first_temp = current_temperature + self.max_diff_for_heat_pump
                self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = first_temp)
                self.climb_target_handle = self.listen_state(self.climb_heat_callback, self.thermostat, attribute='current_temperature')
                self.log(f"Climbing heat up to {target_temp}, initially setting to {first_temp}")
            else:
                self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = target_temp)
                self.log(f"Updated temp since we're home to {target_temp}")
            report_ent.set_state(state='home', attributes=self.today_conf)
        if self.presence_state == 'home' and not any_home:
            self.cancel_climb_heat_mode("presence change to away")
            self.presence_state = 'away'
            self.today_conf['saved_temperature'] = thermostat_state['attributes']['temperature']
            self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = self.today_conf['away'])
            self.log(f"Updated temp since we're away to {self.today_conf['away']} and saved return temp as {self.today_conf['saved_temperature']}")
            report_ent.set_state(state='away', attributes=self.today_conf)

    def cancel_climb_heat_mode(self, reason=None):
        if hasattr(self, 'climb_target_handle'):
            if reason:
                self.log(f"canceling climb mode: {reason}")
            self.cancel_listen_state(self.climb_target_handle)
            del self.climb_target
            del self.climb_target_handle


    @ad.app_lock
    def climb_heat_callback(self, entity, attr, old, new, kwargs):
        next_target = min(self.climb_target, new + self.max_diff_for_heat_pump)
        self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = next_target)
        if next_target >= self.climb_target:
            self.cancel_climb_heat_mode()
            self.log(f"Finished climbing heat up to {target_temp}, since we reached {new}")
        else:
            self.log(f"Climbing heat up to {target_temp}, since we reached {new} we're bumping to {next_target}")

    @ad.app_lock
    def determine_if_warm_or_cool_day(self, kwargs):
        # get temp at noon
        forecasts = self.get_state(self.weather_ent, attribute="forecast")
        noonish_forecast = forecasts[0]
        target_time = datetime.datetime.combine(datetime.date.today(), datetime.time(12,0), pytz.timezone('US/Eastern'))
        for hourly in forecasts:
            sample_time = datetime.datetime.fromisoformat(hourly['datetime'])
            #print(f"Comparing {sample_time} to {target_time}")
            if sample_time >= target_time:
                noonish_forecast = hourly
                break
        noonish_temp = float(noonish_forecast['temperature'])
        #print(f"looking at {self.thermostat} {self.get_state(self.thermostat)}")
        self.today_conf = self.args[self.get_state(self.thermostat)].copy()
        #print(f"today_conf = {self.today_conf}")
        if noonish_temp >= self.today_conf['outside_splitpoint']:
            target_temp = self.today_conf['warm_day']
            self.log(f"Treating today as a warm day")
        else:
            target_temp = self.today_conf['cool_day']
            self.log(f"Treating today as a cool day")
        self.today_conf['target_temp'] = target_temp
        report_ent = self.get_entity(self.report_ent_name)
        report_ent.set_state(state=self.presence_state, attributes=self.today_conf)

    @ad.app_lock
    def wind_down_event(self, event_name, data, kwargs):
        if self.today_conf:
            self.cancel_climb_heat_mode("presence change to sleep")
            self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = self.today_conf['sleep'])

    @ad.app_lock
    def morning_alarm_event(self, event_name, data, kwargs):
        if self.today_conf:
            self.cancel_climb_heat_mode("presence change to morning alarm")
            self.call_service('climate/set_temperature', entity_id = self.thermostat, temperature = self.today_conf['target_temp'])
