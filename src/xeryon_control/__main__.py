import logging

from xeryon_control.reporting.logging_service import start_logging, shutdown_logging


def main():
    start_logging()
    try:
        from xeryon_control.ui.main_window import main as run_desktop
        run_desktop()
    except Exception:
        logging.getLogger(__name__).exception("Application terminated by an exception")
        raise
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
