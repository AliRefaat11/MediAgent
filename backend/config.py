from dataclasses import dataclass


@dataclass
class Config:
    # Scheduling
    min_battery: float = 30            # % needed before a delivery may start
    load_timeout: float = 10           # s to wait for dispenser/status
    load_retries: int = 2
    max_start_delay: float = 60        # s a due dose may wait for robot/dispenser
    # Device heartbeats (robot/state, dispenser/state arrive about every 1 s)
    robot_timeout: float = 10          # s without robot/state = robot offline
    dispenser_timeout: float = 10      # s without dispenser/state = dispenser offline
    # Delivery
    start_timeout: float = 10          # s for the robot to report DELIVERING after "deliver"
    obstacle_limit: int = 3
    delivery_timeout: float = 120
    critical_battery: float = 15
    # Reminder
    reminder_interval: float = 60
    reminder_max_repeats: int = 5
    # Escalation & Notification
    confirm_timeout: float = 600       # s after arrival before the dose is missed
    critical_repeat_interval: float = 120  # s between repeats of an unacked critical
    # Pending doses whose due time is older than this many seconds are marked
    # "missed" instead of being delivered late (e.g. after a restart).
    missed_grace: float = 15 * 60


def demo_config() -> Config:
    return Config(
        reminder_interval=15, confirm_timeout=120, load_timeout=10,
        critical_repeat_interval=60,
    )
