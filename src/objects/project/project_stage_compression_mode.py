from enum import Enum, auto

class StageCompressionMode(Enum):
    rsync = auto()
    lbzip2 = auto()
    bzip2 = auto()
    tar = auto()
    xz = auto()
    pixz = auto()
    gzip = auto()
    squashfs = auto()
