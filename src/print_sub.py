from src.constants import *


class PrintSubmission:
    def __init__(self):
        self.submission_time = None
        self.priority = 5
        self.print_attributes = None
        self.discord_thread = None
        self.status = QUEUED