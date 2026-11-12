"""Observable form values shared by the controller and its presentation."""


class Field:
    def __init__(self, value=""):
        self.value = value
        self._listeners = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for listener in self._listeners:
            listener()

    def subscribe(self, listener):
        self._listeners.append(listener)
