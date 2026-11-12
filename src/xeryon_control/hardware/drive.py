"""CiA-402 controlword sequencing for the Xeryon drive."""
from xeryon_control.hardware.canopen_io import BIT
from xeryon_control.hardware.parameters import NodeOperationMode


def operation_disabled(status):
    return (status & 0x4F) == 0x40 or (status & 0x6F) in (0x21, 0x23)


def check_halted_profile_mode(node, halt_enabled):
    if int(node.sdo[0x6040].raw) != halt_enabled:
        raise RuntimeError("Halt 控制字未确认，采样已取消。")
    if int(node.sdo[0x6061].raw) != NodeOperationMode.PROFILE_POSITION.value:
        raise RuntimeError("位置模式未确认，采样已取消。")


def command_profile_positions(commands, write_controlword, write_target, wait_ack):
    controlword = 0x0F
    nodes = [node for node, _ in commands]
    for node, _ in commands:
        write_controlword(node, controlword)
    # This controller retains bit 12 while idle after enabling (0x1637).
    # Follow the vendor's send-target / request / acknowledge / clear
    # sequence; requiring an idle clear edge prevents any motion.
    for node, target_inc in commands:
        write_target(node, target_inc)
    for node, _ in commands:
        write_controlword(node, controlword | BIT(4))
    wait_ack(nodes, True)
    for node, _ in commands:
        write_controlword(node, controlword)
