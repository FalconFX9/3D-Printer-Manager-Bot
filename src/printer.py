import abc


class Printer(abc.ABC):
    def __init__(self, name):
        self.name = name

    @abc.abstractmethod
    def upload_and_start_print(self, print_sub):
        pass

    @abc.abstractmethod
    def get_status(self):
        pass