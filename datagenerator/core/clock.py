from __future__ import division

from datetime import timedelta

from datagenerator.core.random_generators import *
from datagenerator.core.util_functions import *


class Clock(object):
    """
    A Clock is the central object managing the evolution of time of the whole circus.
    It's generating timestamps on demand, and provides information for TimeProfiler objects.
    """

    def __init__(self, start, step_s, seed, output_format="%Y%m%d %H:%M:%S"):
        """Create a Clock object.

        :type start: pd.Timestamp
        :param start: instant of start of the generation

        :type step_s: int
        :param step_s: number of seconds between each iteration

        :type output_format: string
        :param output_format: format string to return timestamps

        :type seed: int
        :param seed: seed for timestamp generator (if steps are more than 1 sec)

        :return: a new Clock object, initialised
        """

        self.current_date = start
        self.step_s = step_s
        self.step_delta = timedelta(seconds=step_s)

        self.output_format = output_format
        self.__state = RandomState(seed)
        self.ops = self.ClockOps(self)

        # number of clock ticks in one day and in one week
        self.ticks_per_day = 24 * 60 * 60 / step_s
        self.ticks_per_week = self.ticks_per_day * 7

        self.__increment_listeners = []

    def register_increment_listener(self, listener):
        """Add an object to be incremented at each step (such as a TimeProfiler)
        """
        self.__increment_listeners.append(listener)

    def increment(self):
        """Increments the clock by 1 step

        :rtype: NoneType
        :return: None
        """
        self.current_date += self.step_delta

        for listener in self.__increment_listeners:
            listener.increment()

    def get_timestamp(self, size=1):
        """Returns random timestamps within the current value of the clock and the next step

        :type size: int
        :param size: number of timestamps to generate, default 1
        :rtype: Pandas Series
        :return: random timestamps in the form of strings, formatted as defined in the Clock.
        """

        def make_ts(x):
            return (self.current_date + timedelta(seconds=x)).strftime(self.output_format)

        return pd.Series(self.__state.choice(self.step_s, size)).apply(make_ts)

    class ClockOps(object):
        def __init__(self, clock):
            self.clock = clock

        class Timestamp(AddColumns):
            def __init__(self, clock, named_as):
                AddColumns.__init__(self)
                self.clock = clock
                self.named_as = named_as

            def build_output(self, action_data):
                values = self.clock.get_timestamp(action_data.shape[0]).values
                df = pd.DataFrame({self.named_as: values},
                                  index=action_data.index)
                return df

        def timestamp(self, named_as):
            """
            Generates a random timestamp within the current time slice
            """
            return self.Timestamp(self.clock, named_as)


class CyclicTimerGenerator(DependentGenerator):
    """A TimeProfiler contains an activity profile over a defined time range.
    It's mostly a super class, normally only its child classes should be used.

    The goal of a TimeProfiler is to keep a track of the expected level of activity of users over a cyclic time range
    It will store a vector with probabilities of activity per time step, as well as a cumulative sum of the probabilities
    starting with the current time step.

    This allows to quickly produce random waiting times until the next event for the users

    """
    def __init__(self, clock, seed, config):
        """
        This should not be used, only child classes

        :type clock: Clock
        :param clock: the master clock driving this simulator

        :type seed: int
        :param seed: seed for random number generator, default None
        :return: A new TimeProfiler is created
        """
        DependentGenerator.__init__(self)
        self._state = RandomState(seed)

        # "macro" time shift: we shift the whole profile n times in the future
        # or the past until it overlaps with the current clock date
        init_date = latest_date_before(
            starting_date=config.start_date,
            upper_bound=clock.current_date,
            time_step=pd.Timedelta(config.profile_time_steps)*len(
                config.profile))

        # Un-scaled weight profile. We artificially adds a nan to force the
        # up-sclaling to multiply the last element
        profile_idx = pd.date_range(start=init_date,
                                    freq=config.profile_time_steps,
                                    periods=len(config.profile)+1)
        profile_ser = pd.Series(data=config.profile + [np.nan],
                                index=profile_idx)

        # scaled weight profile, s.t. one clock step == one profile value
        profile_ser = profile_ser.resample(rule="%ds" % clock.step_s,
                                           fill_method='pad')[:-1]

        profile_cdf = (profile_ser / profile_ser.sum()).cumsum()
        self.profile = pd.DataFrame({"cdf": profile_cdf,

                                     # for debugging
                                     "timeframe": np.arange(len(profile_cdf))})

        # "micro" time shift,: we step forward along the profile until it is
        # align with the current date
        while self.profile.index[0] < clock.current_date:
            self.increment()

        # makes sure we'll get notified when the clock goes forward
        clock.register_increment_listener(self)

    def increment(self):
        """
        Increment the time generator by 1 step.

        This has as effect to move the cdf of one step to the left, decrease
        all values by the value of the original first entry, and placing the
        previous first entry at the end of the cdf, with value 1.
        """

        self.profile["cdf"] -= self.profile["cdf"].iloc[0]

        self.profile = pd.concat([self.profile.iloc[1:], self.profile.iloc[:1]])
        self.profile.loc[self.profile.index[-1], "cdf"] = 1

    def generate(self, observations):
        """Generate random waiting times, based on some observed activity
        levels. The higher the level of activity, the shorter the waiting
        times will be

        :type observations: Pandas Series
        :param observations: contains an array of floats
        :return: Pandas Series
        """
        # if sum(np.isnan(self._profile["next_prob"])) > 0:
        #     raise Exception("Time profiler is not initialised!")

        draw = self._state.uniform(size=observations.shape[0])
        p = draw / observations.values
        return pd.Series(self.profile["cdf"].searchsorted(p),
                         index=observations.index)


class CyclicTimerProfile(object):
    """
    Static parameters of the Timer profile. Separated from the timer gen
    itself to facilitate persistence.

    :type profile: python array
    :param profile: Weight of each period

    :type profile_time_steps: string
    :param profile_time_steps: duration of the time-steps in the profile
    (e.g. "15min")

    :type start_date: pd.Timestamp
    :param start_date: date of the origin of the specified profile =>
    this is used to align with the values of the clock

    """
    def __init__(self, profile, profile_time_steps, start_date):
        self.start_date = start_date
        self.profile = profile
        self.profile_time_steps = profile_time_steps

    def save_to(self, file_path):

        logging.info("saving timer generator to {}".format(file_path))

        saved_df = pd.DataFrame({("value", "profile"): self.profile},
                                dtype=str).stack()
        saved_df.index = saved_df.index.reorder_levels([1, 0])
        saved_df.loc[("start_date", 0),] = self.start_date
        saved_df.loc[("profile_time_steps", 0),] = self.profile_time_steps
        saved_df.to_csv(file_path)

    @staticmethod
    def load_from(file_path):
        saved_df = pd.read_csv(file_path, index_col=[0, 1])

        profile = saved_df.loc[("profile", slice(None))]\
            .unstack()\
            .astype(float)\
            .tolist()

        profile_time_steps = saved_df.loc["profile_time_steps"].values[0][0]
        start_date = pd.Timestamp(saved_df.loc["start_date"].values[0][0])

        return CyclicTimerProfile(profile, profile_time_steps, start_date)


