"""CANopen node used by offline tests and sequence diagnostics."""
import threading
import time
from types import SimpleNamespace as NS


class Cell:
    def __init__(self, node, index):
        self.node, self.index = node, index

    @property
    def raw(self):
        return self.node.read(self.index)

    @raw.setter
    def raw(self, value):
        self.node.write(self.index, int(value))


class Sdo:
    RESPONSE_TIMEOUT = 1.0
    MAX_RETRIES = 1
    PAUSE_BEFORE_SEND = 0
    names = {"Controlword": 0x6040, "Statusword": 0x6041, "Position Actual Value": 0x6064,
             "Mode of operation": 0x6060, "Modes of Operation Display": 0x6061,
             "Profile velocity": 0x6081, "Profile acceleration": 0x6083,
             "Max profile velocity": 0x607F, "Max acceleration": 0x60C5,
             "Quick stop deceleration": 0x6085, "Following error window": 0x6065,
             "Following error time out": 0x6066, "Position window": 0x6067,
             "Position window time": 0x6068}

    def __init__(self, node):
        self.node = node

    def __getitem__(self, key):
        if key == 0x60A4:
            return {1: Cell(self.node, (0x60A4, 1))}
        if key == "Software position limit":
            return {1: Cell(self.node, (0x607D, 1)), 2: Cell(self.node, (0x607D, 2))}
        return Cell(self.node, self.names[key] if isinstance(key, str) else key)


class Rpdo(dict):
    def __init__(self, node):
        super().__init__({"Controlword": NS(raw=0), "Target Position": NS(raw=node.position)})
        self.node = node

    def transmit(self):
        self.node.write(0x607A, self["Target Position"].raw)
        self.node.write(0x6040, self["Controlword"].raw)


class SimulatedNode:
    def __init__(self, origin=-196):
        self.id = 33
        self.position = origin
        self.registers = {0x6040: 0, 0x6041: 0x40, 0x607A: origin, 0x6060: 1, 0x6081: 5}
        self.lock = threading.RLock()
        self.trajectory = None
        self.commands = []
        self.sdo = Sdo(self)
        self.rpdo = {3: Rpdo(self)}
        self.tpdo = {3: NS(timestamp=None, wait_for_reception=lambda **kwargs: None)}

    @property
    def state(self):
        return "OPERATION ENABLED" if self.read(0x6041) & 0x6F == 0x27 else "SWITCH ON DISABLED"

    def tick(self):
        if self.trajectory is None:
            return
        start, target, started, duration = self.trajectory
        fraction = min(1, (time.monotonic() - started) / duration)
        self.position = round(start + (target - start) * fraction)
        if fraction >= 1:
            self.trajectory = None
            self.registers[0x6041] = 0x427 | (self.registers[0x6041] & 0x1000)

    def read(self, index):
        with self.lock:
            self.tick()
            if index in (0x6064, 0x6062):
                return self.position
            if index == 0x6061:
                return self.registers[0x6060]
            return self.registers.get(index, 0)

    def write(self, index, value):
        with self.lock:
            self.tick()
            previous = self.registers.get(index, 0)
            self.registers[index] = value
            if index != 0x6040:
                return
            if value in (0, 6, 7, 11):
                self.trajectory = None
                self.registers[0x6041] = {0: 0x40, 6: 0x21, 7: 0x23, 11: 0x07}[value]
            elif value & 15 == 15:
                if self.registers[0x6041] & 0x6F != 0x27:
                    self.registers[0x6041] = 0x27
                if value & 16 and not previous & 16:
                    target = self.registers[0x607A]
                    velocity = self.registers[0x6081]
                    duration = max(0.05, min(1, abs(target-self.position) / velocity / 5000))
                    self.trajectory = (self.position, target, time.monotonic(), duration)
                    self.registers[0x6041] = 0x27
                    self.commands.append({"time": time.monotonic(), "target_inc": target,
                                          "velocity_inc": velocity, "start_inc": self.position})
                if value & 16:
                    self.registers[0x6041] |= 0x1000
                else:
                    self.registers[0x6041] &= ~0x1000
