bl_info = {
    "name": "Anim Combiner",
    "author": "Anim Combiner",
    "version": (1, 1, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Anim Combiner",
    "description": "Append, combine, and time-scale armature actions (Blender 5.0 slotted actions)",
    "category": "Animation",
}

import bpy
from bpy.props import (
    PointerProperty, StringProperty, BoolProperty,
    FloatProperty, IntProperty, EnumProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras import anim_utils

def armature_and_action(context):
    obj = context.active_object
    if not obj or obj.type != 'ARMATURE':
        return None, None, None, "Active object must be an Armature"
    ad = obj.animation_data or obj.animation_data_create()
    action = ad.action
    if action is None:
        action = bpy.data.actions.new(name=f"{obj.name}_Combined")
        ad.action = action
    # 5.0: assigning an Action does not auto-pick a slot.
    slot = ad.action_slot
    if slot is None:
        slot = action.slots.new(obj.id_type, obj.name) if hasattr(action.slots, "new") else None
        if slot is None and action.slots:
            slot = action.slots[0]
        ad.action_slot = slot
    if ad.action_slot is None:
        return None, None, None, "Could not assign an Action slot"
    bag = anim_utils.action_ensure_channelbag_for_slot(action, ad.action_slot)
    return obj, action, bag, None

def iter_channelbags(action):
    """Every channelbag on a slotted Action. 5.0 has no action.fcurves."""
    if action is None:
        return
    if not action.layers or not action.slots:
        return
    for layer in action.layers:
        for strip in layer.strips:
            if not hasattr(strip, "channelbag"):
                continue
            for slot in action.slots:
                try:
                    bag = strip.channelbag(slot)
                except Exception:
                    bag = None
                if bag is not None and getattr(bag, "fcurves", None):
                    yield bag

def action_fcurves(action):
    curves = []
    for bag in iter_channelbags(action):
        curves.extend(bag.fcurves)
    return curves

def parse_names(text):
    return {n.strip() for n in text.replace(";", ",").split(",") if n.strip()}

def bone_name_from_path(data_path):
    key = 'pose.bones["'
    i = data_path.find(key)
    if i < 0:
        return None
    i += len(key)
    j = data_path.find('"]', i)
    if j < 0:
        return None
    return data_path[i:j]

def ignored_bone_names(obj, props):
    names = parse_names(props.ignore_bones)
    if props.ignore_collections:
        col_names = parse_names(props.ignore_collection_names)
        collections = getattr(obj.data, "collections_all", None) or getattr(obj.data, "collections", None)
        if collections:
            for col in collections:
                if col.name in col_names:
                    for b in col.bones:
                        names.add(b.name)
    return names

def ensure_fcurve(obj, action, data_path, index):
    # Creates the layer, strip, slot, and curve if needed. Action must already be assigned.
    return action.fcurve_ensure_for_datablock(obj, data_path, index=index)

def copy_action_into(obj, dst_action, src_action, frame_offset, ignore_names):
    copied = 0
    max_frame = frame_offset
    if src_action is None:
        return 0, frame_offset
    for src_fc in action_fcurves(src_action):
        bone = bone_name_from_path(src_fc.data_path)
        if bone is None or bone in ignore_names:
            continue
        dst_fc = ensure_fcurve(obj, dst_action, src_fc.data_path, src_fc.array_index)
        for kp in src_fc.keyframe_points:
            frame = kp.co[0] + frame_offset
            inserted = dst_fc.keyframe_points.insert(frame, kp.co[1], options={'FAST'})
            inserted.interpolation = kp.interpolation
            inserted.easing = kp.easing
            try:
                inserted.handle_left_type = kp.handle_left_type
                inserted.handle_right_type = kp.handle_right_type
                inserted.handle_left = (kp.handle_left[0] + frame_offset, kp.handle_left[1])
                inserted.handle_right = (kp.handle_right[0] + frame_offset, kp.handle_right[1])
            except Exception:
                pass
            copied += 1
            if frame > max_frame:
                max_frame = frame
        dst_fc.update()
    return copied, max_frame

def selected_pose_bone_names(obj):
    # Blender 5.0 stores selection on the pose bone, not Bone.
    if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
        return set()
    return {pb.name for pb in obj.pose.bones if getattr(pb, "select", False)}

def action_range(action):
    curves = action_fcurves(action)
    if not curves:
        return 1.0, 1.0
    start = min(fc.range()[0] for fc in curves)
    end = max(fc.range()[1] for fc in curves)
    return float(start), float(end)

def fit_range(context, action):
    curves = action_fcurves(action)
    if not curves:
        return
    s, e = action_range(action)
    context.scene.frame_start = int(s)
    context.scene.frame_end = max(int(e), int(s) + 1)

class AC_Props(PropertyGroup):
    source_action: PointerProperty(name="Source Action", type=bpy.types.Action)
    ignore_bones: StringProperty(name="Ignore Bones", default="")
    ignore_collections: BoolProperty(name="Ignore Bone Collections", default=False)
    ignore_collection_names: StringProperty(name="Collections", default="")
    add_markers: BoolProperty(name="Marker at Each End", default=True)
    marker_prefix: StringProperty(name="Marker Prefix", default="AC_")
    time_mode: EnumProperty(
        name="Time Edit",
        items=(
            ('OFFSET', "Add Frames", "Shift keys of selected bones from the current frame to the end"),
            ('SCALE', "Scale Time", "Scale key spacing of selected bones from the current frame to the end"),
        ),
        default='OFFSET',
    )
    time_offset: IntProperty(name="Frames", default=10)
    time_scale: FloatProperty(name="Scale", default=1.0, min=0.01, soft_max=5.0)
    autofit: BoolProperty(name="Autofit Start/End", default=True)
    chain_gap: IntProperty(name="Gap", default=0, min=0)

class AC_OT_append(Operator):
    bl_idname = "animcombiner.append_action"
    bl_label = "Append Action at Frame"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.anim_combiner
        obj, dst, _bag, err = armature_and_action(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        src = props.source_action
        if not src:
            self.report({'ERROR'}, "Pick a source action")
            return {'CANCELLED'}
        if src == dst:
            self.report({'ERROR'}, "Source and current action are the same")
            return {'CANCELLED'}

        start, _end = action_range(src)
        offset = context.scene.frame_current - start
        copied, max_frame = copy_action_into(
            obj, dst, src, offset, ignored_bone_names(obj, props)
        )
        if props.add_markers:
            name = f"{props.marker_prefix}{src.name}"
            existing = {m.name for m in context.scene.timeline_markers}
            unique, n = name, 1
            while unique in existing:
                n += 1
                unique = f"{name}.{n:03d}"
            context.scene.timeline_markers.new(unique, frame=int(round(max_frame)))
        if props.autofit:
            fit_range(context, dst)
        self.report({'INFO'}, f"Appended {src.name}: {copied} keys")
        return {'FINISHED'}

class AC_OT_combine_all(Operator):
    bl_idname = "animcombiner.combine_all"
    bl_label = "Combine All Actions"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.anim_combiner
        obj, dst, _bag, err = armature_and_action(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        ignore = ignored_bone_names(obj, props)
        cursor = action_range(dst)[1] if action_fcurves(dst) else float(context.scene.frame_current)
        total = used = 0
        for src in list(bpy.data.actions):
            if src == dst or not action_fcurves(src):
                continue
            if not any(bone_name_from_path(fc.data_path) for fc in action_fcurves(src)):
                continue
            start, _end = action_range(src)
            copied, max_frame = copy_action_into(obj, dst, src, cursor - start, ignore)
            if not copied:
                continue
            used += 1
            total += copied
            if props.add_markers:
                context.scene.timeline_markers.new(
                    f"{props.marker_prefix}{src.name}", frame=int(round(max_frame))
                )
            cursor = max_frame + props.chain_gap + 1.0
        if props.autofit:
            fit_range(context, dst)
        self.report({'INFO'}, f"Combined {used} actions, {total} keys")
        return {'FINISHED'}

class AC_OT_time_edit(Operator):
    bl_idname = "animcombiner.time_edit"
    bl_label = "Apply Time Edit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.anim_combiner
        obj, action, bag, err = armature_and_action(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        names = selected_pose_bone_names(obj)
        if not names:
            self.report({'ERROR'}, "Select at least one pose bone")
            return {'CANCELLED'}
        pivot = float(context.scene.frame_current)
        changed = 0
        for fc in bag.fcurves:
            if bone_name_from_path(fc.data_path) not in names:
                continue
            for kp in sorted(fc.keyframe_points, key=lambda k: k.co[0], reverse=True):
                if kp.co[0] < pivot - 1e-4:
                    continue
                if props.time_mode == 'OFFSET':
                    delta = float(props.time_offset)
                    kp.co[0] += delta
                    kp.handle_left[0] += delta
                    kp.handle_right[0] += delta
                else:
                    scale = props.time_scale
                    remap = lambda x, p=pivot, s=scale: p + (x - p) * s
                    kp.co[0] = remap(kp.co[0])
                    kp.handle_left[0] = remap(kp.handle_left[0])
                    kp.handle_right[0] = remap(kp.handle_right[0])
                changed += 1
            fc.update()
        if props.autofit:
            fit_range(context, action)
        self.report({'INFO'}, f"Edited {changed} keys on {len(names)} bones")
        return {'FINISHED'}

class AC_OT_autofit(Operator):
    bl_idname = "animcombiner.autofit"
    bl_label = "Autofit Frame Range"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _obj, action, _bag, err = armature_and_action(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        if not action_fcurves(action):
            self.report({'WARNING'}, "Action has no keys")
            return {'CANCELLED'}
        fit_range(context, action)
        return {'FINISHED'}

class AC_PT_panel(Panel):
    bl_label = "Anim Combiner"
    bl_idname = "AC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Anim Combiner"

    def draw(self, context):
        layout = self.layout
        props = context.scene.anim_combiner
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        ad = obj.animation_data
        current = ad.action if ad else None
        col = layout.column(align=True)
        col.label(text=f"Armature: {obj.name}", icon='ARMATURE_DATA')
        col.label(text=f"Current: {current.name if current else 'None'}", icon='ACTION')
        slot = ad.action_slot if ad else None
        col.label(text=f"Slot: {slot.name_display if slot else 'None'}", icon='ACTION_SLOT')

        box = layout.box()
        box.label(text="Append", icon='IMPORT')
        box.prop_search(props, "source_action", bpy.data, "actions", text="")
        row = box.row(align=True)
        row.prop(props, "add_markers", text="End Marker")
        row.prop(props, "marker_prefix", text="")
        box.operator("animcombiner.append_action", icon='KEYFRAME')

        box = layout.box()
        box.label(text="Combine All", icon='SEQ_SEQUENCER')
        box.prop(props, "chain_gap")
        box.operator("animcombiner.combine_all", icon='ACTION_TWEAK')

        box = layout.box()
        box.label(text="Ignore", icon='X')
        box.prop(props, "ignore_bones", text="Bones")
        box.prop(props, "ignore_collections")
        if props.ignore_collections:
            box.prop(props, "ignore_collection_names", text="Collections")

        box = layout.box()
        box.label(text="Time Edit (selected bones)", icon='TIME')
        box.prop(props, "time_mode", text="")
        box.prop(props, "time_offset" if props.time_mode == 'OFFSET' else "time_scale")
        box.operator("animcombiner.time_edit", icon='MOD_TIME')

        box = layout.box()
        box.label(text="Frame Range", icon='PREVIEW_RANGE')
        box.prop(props, "autofit")
        box.operator("animcombiner.autofit", icon='FILE_REFRESH')

classes = (
    AC_Props, AC_OT_append, AC_OT_combine_all,
    AC_OT_time_edit, AC_OT_autofit, AC_PT_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.anim_combiner = PointerProperty(type=AC_Props)

def unregister():
    del bpy.types.Scene.anim_combiner
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()