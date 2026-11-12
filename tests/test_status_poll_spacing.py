"""Status polling must leave command admission time and never overlap reads."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from xeryon_control.application.session import ControllerSession as Gui, STATUS_POLL_REST_SECONDS


class StatusPollSpacingTests(unittest.TestCase):
    def app(self):
        return NS(node=object(), status_busy=False, command_busy=False,
                  stop_pending=False, stop_unconfirmed=False,
                  next_status_poll_time=10.1, schedule=Mock(),
                  _poll_status=Mock(), _status_worker=Mock())

    def test_no_read_during_reserved_idle_window(self):
        app=self.app()
        with patch('xeryon_control.application.feedback.time.monotonic', return_value=10), patch('xeryon_control.application.feedback.threading.Thread') as thread:
            Gui._poll_status(app)
        thread.assert_not_called()
        app.schedule.assert_called_once()
        self.assertFalse(app.status_busy)

    def test_read_resumes_after_idle_without_overlapping(self):
        app=self.app()
        with patch('xeryon_control.application.feedback.time.monotonic', return_value=10.2), patch('xeryon_control.application.feedback.threading.Thread') as thread:
            Gui._poll_status(app)
            Gui._poll_status(app)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertTrue(app.status_busy)

    def test_worker_exit_reserves_idle_before_clearing_busy(self):
        app=NS(node=None, nodes={}, io_lock=threading.RLock(), status_busy=True,
               _active_axis_name=Mock(return_value='Actuator A'))
        with patch('xeryon_control.application.feedback.time.monotonic', return_value=12):
            Gui._status_worker(app)
        self.assertFalse(app.status_busy)
        self.assertEqual(app.next_status_poll_time,12+STATUS_POLL_REST_SECONDS)


if __name__=='__main__':
    unittest.main()
