from .dataset import DatasetTemplate


class NordLand(DatasetTemplate):
    def __init__(self, config, mode):
        super().__init__(config, mode)