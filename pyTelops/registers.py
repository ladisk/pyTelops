"""
Telops FAST M3k register addresses and enum mappings.

Source: GenICam XML (CommonTEL2000LibProject.xml v12.7.1) downloaded from camera.
Register values are 4 bytes. Integers are big-endian uint32 (>I).
Floats are big-endian IEEE 754 (>f).
"""

from enum import IntEnum

# ============================================================
# Stream Channel 0 Registers (GigE Vision standard)
# ============================================================
REG_SC_HOST_PORT = 0x0D00
REG_SC_PACKET_SIZE = 0x0D04
REG_SC_PACKET_DELAY = 0x0D08
REG_SC_DEST_ADDR = 0x0D18

# ============================================================
# Image Format
# ============================================================
REG_WIDTH = 0xD300
REG_HEIGHT = 0xD304  # Includes 2 header rows
REG_PIXEL_FORMAT = 0xD308
REG_PAYLOAD_SIZE = 0xD30C
REG_ACQUISITION_MODE = 0xD310
REG_ACQUISITION_START = 0xD314  # Command
REG_ACQUISITION_STOP = 0xD318  # Command
REG_ACQUISITION_ARM = 0xE800  # Command

# ============================================================
# Exposure & Frame Rate
# ============================================================
REG_EXPOSURE_MODE = 0xE804
REG_EXPOSURE_TIME = 0xE808  # Float, microseconds
REG_EXPOSURE_AUTO = 0xE82C  # ExposureAuto: 0=Off, 1=Once, 2=Continuous
REG_ACQUISITION_FRAME_RATE = 0xE810  # Float, Hz
REG_EXPOSURE_TIME_1 = 0xE84C  # EHDRI exposure 1
REG_EXPOSURE_TIME_2 = 0xE850
REG_EXPOSURE_TIME_3 = 0xE854
REG_EXPOSURE_TIME_4 = 0xE858

# ============================================================
# Detector / Sensor
# ============================================================
REG_SENSOR_WELL_DEPTH = 0xE8DC
REG_INTEGRATION_MODE = 0xE8E0
REG_CALIBRATION_MODE = 0xE86C
REG_DETECTOR_MODE = 0xEC38

# ============================================================
# Trigger
# ============================================================
REG_TRIGGER_SELECTOR = 0xE8F0
REG_TRIGGER_MODE = 0xE8F4
REG_TRIGGER_SOFTWARE = 0xE8F8  # Command
REG_TRIGGER_SOURCE = 0xE8FC
REG_TRIGGER_ACTIVATION = 0xE900
REG_TRIGGER_DELAY = 0xE904  # Float, microseconds

# ============================================================
# Memory Buffer (16GB onboard)
# ============================================================
REG_MEMORY_BUFFER_MODE = 0xE908
REG_MEMORY_BUFFER_NUM_IMAGES_MAX = 0xE90C
REG_MEMORY_BUFFER_NUM_SEQ_MAX = 0xE910
REG_MEMORY_BUFFER_SEQ_COUNT = 0xE914
REG_MEMORY_BUFFER_NUM_SEQUENCES = 0xE918
REG_MEMORY_BUFFER_SEQ_SIZE_MAX = 0xE91C
REG_MEMORY_BUFFER_SEQ_SIZE = 0xE920
REG_MEMORY_BUFFER_PRE_MOI_SIZE = 0xE924
REG_MEMORY_BUFFER_SEQ_SELECTOR = 0xE928
REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID = 0xE92C
REG_MEMORY_BUFFER_SEQ_MOI_FRAME_ID = 0xE930
REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE = 0xE934
REG_MEMORY_BUFFER_DOWNLOAD_IMAGE_FRAME_ID = 0xE938
REG_MEMORY_BUFFER_DOWNLOAD_MODE = 0xE93C
REG_MEMORY_BUFFER_CLEAR_ALL = 0xE940  # Command

# Memory Buffer - Extended
REG_MEMORY_BUFFER_MOI_SOURCE = 0xEB2C
REG_MEMORY_BUFFER_MOI_ACTIVATION = 0xEB30
REG_MEMORY_BUFFER_MOI_SOFTWARE = 0xEB34  # Command
REG_MEMORY_BUFFER_STATUS = 0xEBB0
REG_MEMORY_BUFFER_SEQ_CLEAR = 0xEB98  # Command
REG_MEMORY_BUFFER_SEQ_DEFRAG = 0xEB9C  # Command
REG_MEMORY_BUFFER_SEQ_WIDTH = 0xEB90
REG_MEMORY_BUFFER_SEQ_HEIGHT = 0xEB94
REG_MEMORY_BUFFER_SEQ_OFFSET_X = 0xEB88
REG_MEMORY_BUFFER_SEQ_OFFSET_Y = 0xEB8C
REG_MEMORY_BUFFER_DOWNLOAD_FRAME_COUNT = 0xEBAC
REG_MEMORY_BUFFER_DOWNLOAD_FRAME_ID = 0xEBA8
REG_MEMORY_BUFFER_DOWNLOAD_BITRATE_MAX = 0xEAD4  # Float
REG_MEMORY_BUFFER_TOTAL_SPACE_HIGH = 0xEB80
REG_MEMORY_BUFFER_TOTAL_SPACE_LOW = 0xEB84
REG_MEMORY_BUFFER_FREE_SPACE_HIGH = 0xEB70
REG_MEMORY_BUFFER_FREE_SPACE_LOW = 0xEB74

# ============================================================
# Device Control / Status
# ============================================================
REG_DEVICE_RESET = 0xE948  # Command
REG_DEVICE_POWER_STATE = 0xE94C
REG_DEVICE_LED = 0xE950
REG_DEVICE_NOT_READY = 0xEA84
REG_DEVICE_TEMPERATURE = 0xE970  # Float, Celsius

# ============================================================
# Enumerations
# ============================================================


class CalibrationMode(IntEnum):
    RAW0 = 0
    RAW = 255
    NUC = 1
    RT = 2       # Radiometric temperature (Celsius)
    IBR = 3      # In-band radiance
    IBI = 4      # In-band irradiance


class ExposureAuto(IntEnum):
    OFF = 0
    ONCE = 1
    CONTINUOUS = 2


class ExposureMode(IntEnum):
    OFF = 0
    TIMED = 1
    TRIGGER_WIDTH = 2
    TRIGGER_CONTROLLED = 3


class AcquisitionMode(IntEnum):
    CONTINUOUS = 0
    SINGLE_FRAME = 1
    MULTI_FRAME = 2


class TriggerSelector(IntEnum):
    ACQUISITION_START = 0
    FLAGGING = 1
    GATING = 2


class TriggerMode(IntEnum):
    OFF = 0
    ON = 1


class TriggerSource(IntEnum):
    SOFTWARE = 0
    EXTERNAL_SIGNAL = 48  # BNC connector


class TriggerActivation(IntEnum):
    RISING_EDGE = 0
    FALLING_EDGE = 1
    ANY_EDGE = 2
    LEVEL_HIGH = 3
    LEVEL_LOW = 4


class MemoryBufferMode(IntEnum):
    OFF = 0
    ON = 1


class MemoryBufferStatus(IntEnum):
    DEACTIVATED = 0
    IDLE = 1
    HOLDING = 2
    RECORDING = 3
    UPDATING = 4
    TRANSMITTING = 5
    DEFRAGGING = 6
    REFRESH = 255


class MemoryBufferDownloadMode(IntEnum):
    OFF = 0
    SEQUENCE = 1
    IMAGE = 2


class MemoryBufferMOISource(IntEnum):
    ACQUISITION_STARTED = 0
    SOFTWARE = 1
    EXTERNAL_SIGNAL = 2
    NONE = 255


class MemoryBufferMOIActivation(IntEnum):
    RISING_EDGE = 0
    FALLING_EDGE = 1
    ANY_EDGE = 2


class IntegrationMode(IntEnum):
    INTEGRATE_THEN_READ = 0
    INTEGRATE_WHILE_READ = 1


class DetectorMode(IntEnum):
    NORMAL = 0
    BURST = 1


class SensorWellDepth(IntEnum):
    LOW_GAIN = 0
    HIGH_GAIN = 1


class DevicePowerState(IntEnum):
    STANDBY = 0
    ON = 1
    IN_TRANSITION = 2


class PixelFormat(IntEnum):
    MONO8 = 0x01080001
    MONO16 = 0x01100007
    MONO10 = 0x01100003
    MONO12 = 0x01100005
    MONO14 = 0x01100025


# ============================================================
# Register metadata: address -> (name, type, access)
# ============================================================
REGISTER_INFO = {
    REG_WIDTH: ("Width", "int", "RW"),
    REG_HEIGHT: ("Height", "int", "RW"),
    REG_PIXEL_FORMAT: ("PixelFormat", "enum", "RW"),
    REG_PAYLOAD_SIZE: ("PayloadSize", "int", "RO"),
    REG_ACQUISITION_MODE: ("AcquisitionMode", "enum", "RW"),
    REG_ACQUISITION_START: ("AcquisitionStart", "cmd", "WO"),
    REG_ACQUISITION_STOP: ("AcquisitionStop", "cmd", "WO"),
    REG_ACQUISITION_ARM: ("AcquisitionArm", "cmd", "WO"),
    REG_EXPOSURE_MODE: ("ExposureMode", "enum", "RW"),
    REG_EXPOSURE_TIME: ("ExposureTime", "float", "RW"),
    REG_ACQUISITION_FRAME_RATE: ("AcquisitionFrameRate", "float", "RW"),
    REG_CALIBRATION_MODE: ("CalibrationMode", "enum", "RW"),
    REG_INTEGRATION_MODE: ("IntegrationMode", "enum", "RW"),
    REG_DETECTOR_MODE: ("DetectorMode", "enum", "RW"),
    REG_SENSOR_WELL_DEPTH: ("SensorWellDepth", "enum", "RW"),
    REG_TRIGGER_SELECTOR: ("TriggerSelector", "enum", "RW"),
    REG_TRIGGER_MODE: ("TriggerMode", "enum", "RW"),
    REG_TRIGGER_SOFTWARE: ("TriggerSoftware", "cmd", "WO"),
    REG_TRIGGER_SOURCE: ("TriggerSource", "enum", "RW"),
    REG_TRIGGER_ACTIVATION: ("TriggerActivation", "enum", "RW"),
    REG_TRIGGER_DELAY: ("TriggerDelay", "float", "RW"),
    REG_MEMORY_BUFFER_MODE: ("MemoryBufferMode", "enum", "RW"),
    REG_MEMORY_BUFFER_SEQ_SIZE: ("MemoryBufferSequenceSize", "int", "RW"),
    REG_MEMORY_BUFFER_DOWNLOAD_MODE: ("MemoryBufferDownloadMode", "enum", "RW"),
    REG_MEMORY_BUFFER_STATUS: ("MemoryBufferStatus", "enum", "RO"),
    REG_DEVICE_POWER_STATE: ("DevicePowerState", "enum", "RO"),
    REG_DEVICE_NOT_READY: ("DeviceNotReady", "bool", "RO"),
    REG_DEVICE_TEMPERATURE: ("DeviceTemperature", "float", "RO"),
    REG_DEVICE_RESET: ("DeviceReset", "cmd", "WO"),
}

FEATURE_TO_ADDRESS = {v[0]: k for k, v in REGISTER_INFO.items()}
