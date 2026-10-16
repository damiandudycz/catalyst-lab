from __future__ import annotations
from .deploy_installation import DeployStep
from .deploy_target import PartitionTable
from .deploy_image import (
    create_image_script, shrink_image_script, compress_image_script, grow_root_script
)

# ------------------------------------------------------------------------------
# Steps of deploying to disk image file (see deploy_image).

class DeployStepCreateImage(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Create image", description="Connects to the image builder, checks tools and creates a temporary image",
                         multistage_process=multistage_process)
    def run(self):
        import os
        from .ssh_connection import quote
        process = self.multistage_process
        plan = process.plan
        if not process.connection.is_alive():
            self.log(f"Connecting to {process.connection.host}")
            error = process.connection.connect()
            if error:
                raise RuntimeError(error)
        if not os.path.isfile(process.build.artifact_path or ""):
            raise RuntimeError("Build archive doesn't exist anymore")
        tools = " ".join(quote(tool) for tool in plan.required_tools())
        self.remote(f"""
missing=""
for tool in {tools}; do command -v "$tool" > /dev/null || missing="$missing $tool"; done
[ -z "$missing" ] || {{ echo "Missing tools:$missing"; exit 1; }}
""", "Tools needed to create image are missing")
        lines = []
        def handle(line: str):
            lines.append(line)
            if not line.startswith("LOOP="):
                self.log(line)
        if not process.connection.run(create_image_script(plan.image_path, plan.disk.size), handle, self.processes):
            raise RuntimeError("Failed to create image")
        loop = next((line[len("LOOP="):].strip() for line in lines if line.startswith("LOOP=")), None)
        if not loop:
            raise RuntimeError("Image was not attached as loop device")
        # Next steps partition, format and mount loop device, like disk.
        plan.disk.path = loop

class DeployStepGrowRoot(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Grow at first start", description="Installs service growing root partition to fill disk",
                         multistage_process=multistage_process)
    def run(self):
        from .ssh_connection import quote
        self.remote(f"ROOT={quote(self.multistage_process.root)}\n" + grow_root_script(), "Failed to install root growing service")

class DeployStepShrinkImage(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Shrink image", description="Shrinks root filesystem to its contents and cuts image after it",
                         multistage_process=multistage_process)
    def run(self):
        plan = self.multistage_process.plan
        index = plan.root_index
        self.remote(shrink_image_script(plan.image_path, plan.disk.path, plan.partition_device(index), index,
                                        gpt=plan.table == PartitionTable.GPT), "Failed to shrink image")

class DeployStepCompressImage(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Compress image", description="Compresses image with xz", multistage_process=multistage_process)
    def run(self):
        self.remote(compress_image_script(self.multistage_process.plan.image_path), "Failed to compress image")

class DeployStepSaveImage(DeployStep):
    def __init__(self, multistage_process):
        super().__init__(name="Save image", description="Moves image to selected location", multistage_process=multistage_process)
    def run(self):
        import os, shutil
        process = self.multistage_process
        source, destination = process.created_image_path, process.output_path
        if not destination or destination == source:
            self.log(f"Image: {source}")
            return
        if not os.path.isfile(source):
            raise RuntimeError("Created image doesn't exist")
        # Moved within the same disk, copied to other one (with progress) and removed.
        try:
            os.replace(source, destination)
        except OSError:
            size = os.path.getsize(source) or 1
            copied = 0
            with open(source, "rb") as input_file, open(destination + ".part", "wb") as output_file:
                while chunk := input_file.read(8 * 1024 * 1024):
                    output_file.write(chunk)
                    copied += len(chunk)
                    self._update_progress(copied / size * 0.99)
            os.replace(destination + ".part", destination)
            os.remove(source)
        self.log(f"Image: {destination}")
