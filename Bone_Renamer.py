bl_info = {
    "name": "Bone Renamer",
    "author": "Bone Renamer",
    "version": (1, 2, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Bone Renamer",
    "description": "Rename armature bones from a two-column text file and export bone names",
    "category": "Rigging",
}

import bpy
import os
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator, Panel

def get_armature(context):
    obj = context.active_object
    if obj and obj.type == 'ARMATURE':
        return obj
    return None

def safe_name(name):
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip() or "Armature"

def collection_name(arm_obj):
    cols = list(arm_obj.users_collection)
    if not cols:
        return "Scene"
    # Prefer a real collection over the scene master collection
    scene_col = bpy.context.scene.collection if bpy.context.scene else None
    for col in cols:
        if col != scene_col:
            return col.name
    return cols[0].name

def load_mapping(filepath, reverse=False):
    """Sheets paste: column A <TAB> column B. Comma also accepted."""
    mapping = {}
    with open(filepath, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t")
            elif "," in line:
                parts = line.split(",")
            else:
                continue
            if len(parts) < 2:
                continue
            a = parts[0].strip().strip('"').strip("'")
            b = parts[1].strip().strip('"').strip("'")
            if not a or not b:
                continue
            mapping[b if reverse else a] = a if reverse else b
    return mapping

class BONE_OT_rename_from_file(Operator):
    bl_idname = "bone_renamer.rename_from_file"
    bl_label = "Rename Bones"
    bl_description = "Rename bones using the selected two-column text file"
    bl_options = {"REGISTER", "UNDO"}

    reverse: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return get_armature(context) is not None

    def execute(self, context):
        arm_obj = get_armature(context)
        filepath = context.scene.bone_renamer_filepath
        if not filepath or not os.path.isfile(bpy.path.abspath(filepath)):
            self.report({"ERROR"}, "Select a valid .txt file")
            return {"CANCELLED"}

        filepath = bpy.path.abspath(filepath)
        try:
            mapping = load_mapping(filepath, reverse=self.reverse)
        except OSError as e:
            self.report({"ERROR"}, f"Could not read file: {e}")
            return {"CANCELLED"}

        if not mapping:
            self.report({"ERROR"}, "No name pairs found. Use tab-separated: old<TAB>new")
            return {"CANCELLED"}

        bones = arm_obj.data.bones
        planned = []
        for bone in bones:
            if bone.name in mapping:
                target = mapping[bone.name]
                if target and target != bone.name:
                    planned.append((bone.name, target))

        temps = {}
        for i, (old, new) in enumerate(planned):
            bone = bones.get(old)
            if bone is None:
                continue
            tmp = f"__br_tmp__{i}"
            bone.name = tmp
            temps[tmp] = new

        clashes = 0
        for tmp, new in temps.items():
            bone = bones.get(tmp)
            if bone is None:
                continue
            if new in bones:
                clashes += 1
            bone.name = new

        direction = "back" if self.reverse else "forward"
        msg = f"Renamed {len(temps)} bones ({direction})"
        if clashes:
            msg += f", {clashes} targets already existed (Blender may have added .001)"
        self.report({"INFO"}, msg)
        return {"FINISHED"}

class BONE_OT_export_names(Operator):
    bl_idname = "bone_renamer.export_names"
    bl_label = "Export Bone Names"
    bl_description = "Write bone names only, as <Collection>_<blend name>.txt next to the .blend"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return get_armature(context) is not None

    def execute(self, context):
        arm_obj = get_armature(context)
        blend = bpy.data.filepath
        if not blend:
            self.report({"ERROR"}, "Save the .blend file first")
            return {"CANCELLED"}

        folder = os.path.dirname(blend)
        blend_name = os.path.splitext(os.path.basename(blend))[0]
        filename = f"{safe_name(collection_name(arm_obj))}_{safe_name(blend_name)}.txt"
        out_path = os.path.join(folder, filename)

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                for bone in arm_obj.data.bones:
                    f.write(bone.name + "\n")
        except OSError as e:
            self.report({"ERROR"}, f"Export failed: {e}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Exported {len(arm_obj.data.bones)} bones to {out_path}")
        return {"FINISHED"}

class BONE_PT_renamer(Panel):
    bl_label = "Bone Renamer"
    bl_idname = "BONE_PT_renamer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Bone Renamer"

    @classmethod
    def poll(cls, context):
        return get_armature(context) is not None

    def draw(self, context):
        layout = self.layout
        arm = get_armature(context)
        layout.label(text=f"Armature: {arm.name}", icon="ARMATURE_DATA")
        layout.label(text=f"Collection: {collection_name(arm)}")
        layout.separator()
        layout.prop(context.scene, "bone_renamer_filepath", text="")
        row = layout.row(align=True)
        op = row.operator("bone_renamer.rename_from_file", text="Rename", icon="SORTALPHA")
        op.reverse = False
        op = row.operator("bone_renamer.rename_from_file", text="Rename Back", icon="LOOP_BACK")
        op.reverse = True
        layout.separator()
        layout.operator("bone_renamer.export_names", icon="EXPORT")

classes = (
    BONE_OT_rename_from_file,
    BONE_OT_export_names,
    BONE_PT_renamer,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bone_renamer_filepath = StringProperty(
        name="Rename File",
        description="Text file: column A old name, column B new name (tab-separated)",
        subtype="FILE_PATH",
        default="",
    )

def unregister():
    del bpy.types.Scene.bone_renamer_filepath
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()