import logging


def get_file_logger(name: str, log_file: str, level=logging.INFO) -> logging.Logger:
    """Configures a file-only logger that clears the log on each run and omits timestamps."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        # mode="w" overwrites/clears the log file on script start
        file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="w")

        # Simple format containing only the log message
        formatter = logging.Formatter(fmt="%(message)s")

        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger