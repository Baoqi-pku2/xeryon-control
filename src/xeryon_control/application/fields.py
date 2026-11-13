"""Observable form values shared by the controller and its presentation."""


class Field:
    def __init__(self, value=""):
        self.value = value
        self._listeners = []
        self._edit_listeners = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for listener in self._listeners:
            listener()

    def subscribe(self, listener):
        self._listeners.append(listener)

    def set_from_user(self, value):
        self.set(value)
        for listener in self._edit_listeners:
            listener(self.value)

    def subscribe_edits(self, listener):
        self._edit_listeners.append(listener)
