from dataclasses import dataclass


@dataclass
class Config:
    min_battery: float = 30
    load_timeout: float = 10
    load_retries: int = 2
    max_start_delay: float = 60
    robot_timeout: float = 10
    obstacle_limit: int = 3
    delivery_timeout: float = 120
    critical_battery: float = 15
    reminder_interval: float = 60
    reminder_max_repeats: int = 5
    confirm_timeout: float = 600
    # Pending doses whose due time is older than this many seconds are marked
    # "missed" instead of being delivered late (e.g. after a restart).
    missed_grace: float = 15 * 60


def demo_config() -> Config:
    return Config(reminder_interval=15, confirm_timeout=120, load_timeout=10)
