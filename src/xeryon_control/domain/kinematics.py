"""Fixed-lag linear Gaussian smoother for time-stamped encoder positions."""

from collections import deque
from dataclasses import dataclass


@dataclass
class FilterStep:
    time: float
    measured: float
    state: list
    covariance: list
    dt: float
    gain: list
    scaled_residual: float


class EncoderSmoother:
    """Constant-acceleration model driven by continuous white jerk, in µm and s."""

    lag = 0.5

    def __init__(self, resolution):
        self.variance = max(resolution ** 2 / 12, 1e-10)
        # Nominal process noise spectral density in µm²/s⁵ (display model).
        self.jerk_density = 625.0
        self.pending = deque()
        self.count = 0

    def add(self, t, position):
        if not self.pending:
            prior = [[self.variance, 0, 0], [0, 500**2, 0], [0, 0, 1000**2]]
            step = FilterStep(t, position, [position, 0, 0], prior, 0, [0]*3, 0)
        else:
            last = self.pending[-1]
            dt = t - last.time
            if dt <= 0:
                return []
            step = self._update(last, dt, t, position)
        self.count += 1
        self.pending.append(step)
        if t - self.pending[0].time < self.lag:
            return []
        states = self._smooth()
        ready = []
        for state in states:
            if t - self.pending[0].time < self.lag:
                break
            self.pending.popleft()
            ready.append(state)
        return ready if self.count >= 3 else []

    def finish(self):
        states = self._smooth() if self.count >= 3 else []
        self.pending.clear()
        self.count = 0
        return states

    def _update(self, last, dt, t, measurement):
        # P_pred = F P F' + Q; Q integrates white jerk over the actual interval.
        half = dt**2 / 2
        f = ((1, dt, half), (0, 1, dt), (0, 0, 1))
        q = ((dt**5/20, dt**4/8, dt**3/6),
             (dt**4/8, dt**3/3, half), (dt**3/6, half, dt))
        fp = [[sum(f[i][k] * last.covariance[k][j] for k in range(3))
               for j in range(3)] for i in range(3)]
        pred = [[sum(fp[i][k] * f[j][k] for k in range(3)) + self.jerk_density*q[i][j]
                 for j in range(3)] for i in range(3)]
        p, v, a = last.state
        state = [p + dt*v + half*a, v + dt*a, a]
        residual = measurement - state[0]
        innovation_var = pred[0][0] + self.variance
        gain = [r[0] / innovation_var for r in pred]
        state = [x + g * residual for x, g in zip(state, gain)]
        cov = [[pred[i][j] - gain[i]*pred[0][j] for j in range(3)] for i in range(3)]
        return FilterStep(t, measurement, state, cov, dt, gain, residual / innovation_var)

    def _smooth(self):
        # Backward information-vector form of the RTS smoothed mean.
        # Only means are needed, so no inverse covariance matrices are required.
        r = [0.0]*3
        result = []
        for step in reversed(self.pending):
            state = [x + sum(p*b for p, b in zip(row, r))
                     for x, row in zip(step.state, step.covariance)]
            result.append((step.time, step.measured, state[1], state[2]))
            dt = step.dt
            h = r[0] + step.scaled_residual - sum(g*b for g, b in zip(step.gain, r))
            r = [h, dt*h + r[1], dt**2/2*h + dt*r[1] + r[2]]
        return list(reversed(result))
