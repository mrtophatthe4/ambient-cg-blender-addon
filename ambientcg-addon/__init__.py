bl_info = {
    "name": "AmbientCG Asset Browser",
    "blender": (3, 0, 0),
    "category": "3D View",
    "author": "mrtophatthe4",
    "version": (0, 0, 1),
    "location": "View3D > UI > Assets",
    "description": "Browse and import AmbientCG materials directly from Blender.",
    "warning": "",
    "wiki_url": "",
    "tracker_url": "",
}

import os
import re
import bpy
import requests
import urllib.request
import zipfile
import webbrowser
from pathlib import Path
import bpy.utils.previews

# Global dictionary to track downloaded assets
downloaded_assets = {}  # keys: asset_id, value: True if material created

# Global preview collection for thumbnails
preview_collections = {}

# -------------------------------------------------------------------
# Custom URL operator (to open asset pages)
# -------------------------------------------------------------------
class URL_OT_Open(bpy.types.Operator):
    bl_idname = "url.open"
    bl_label = "Open URL"
    
    url: bpy.props.StringProperty(default="")

    def execute(self, context):
        webbrowser.open(self.url)
        return {'FINISHED'}

# -------------------------------------------------------------------
# Fetch asset data from AmbientCG using regex
# -------------------------------------------------------------------
ASSET_LIST_URL = (
    "https://ambientcg.com/hx/asset-list?id=&childrenOf=&variationsOf=&parentsOf="
    "&q=ball&colorMode=&thumbnails=200&sort=popular"
)
response = requests.get(ASSET_LIST_URL)
asset_pattern = re.compile(r'<div class="asset-block" id="asset-([^"]+)">')
link_pattern  = re.compile(r'<a\s+href="(/view\?id=[^"]+)">')
img_pattern   = re.compile(r'<img[^>]+class="only-show-dark"[^>]+src="([^"]+)"')
assets = []
for match in zip(asset_pattern.finditer(response.text),
                 link_pattern.finditer(response.text),
                 img_pattern.finditer(response.text)):
    asset_id = match[0].group(1)
    asset_link = match[1].group(1)
    asset_img = match[2].group(1)
    assets.append((asset_id, asset_link, asset_img))

# -------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------
def get_cache_dir():
    home = Path.home()
    cache_dir = home / ".cache" / "ambientcg"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

def fetch_and_create_material(material_name, resolution):
    """
    Synchronously downloads and extracts the asset ZIP file.
    Returns a Path object (the extraction folder) on success,
    or a string (error message) on failure.
    """
    url = f"https://ambientcg.com/get?file={material_name}_{resolution}-PNG.zip"
    cache_dir = get_cache_dir()
    extract_path = cache_dir / f"{material_name}_{resolution}"
    
    if not extract_path.exists():
        zip_path = cache_dir / f"{material_name}_{resolution}.zip"
        opener = urllib.request.build_opener()
        opener.addheaders = [
            ("User-Agent",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/91.0.4472.124 Safari/537.36")
        ]
        urllib.request.install_opener(opener)
        try:
            urllib.request.urlretrieve(url, zip_path)
        except Exception as e:
            return f"Failed to download file: {str(e)}"
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(extract_path)
            zip_path.unlink()  # Remove the ZIP file after extraction
        except Exception as e:
            return f"Failed to extract zip file: {str(e)}"
    return extract_path

def create_material_from_extracted(extract_path, asset_name):
    """
    Creates a new material using textures found in the extracted folder.
    """
    material = bpy.data.materials.new(name=asset_name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    material_output = nodes.new(type="ShaderNodeOutputMaterial")
    material_output.location = (300, 0)
    principled = nodes.new(type="ShaderNodeBsdfPrincipled")
    principled.location = (0, 0)
    links.new(principled.outputs["BSDF"], material_output.inputs["Surface"])
    for file in os.listdir(extract_path):
        file_path = extract_path / file
        if file.endswith("_Color.png"):
            color_tex = nodes.new(type="ShaderNodeTexImage")
            color_tex.location = (-600, 300)
            color_tex.image = bpy.data.images.load(str(file_path))
            color_tex.image.colorspace_settings.name = "sRGB"
            links.new(color_tex.outputs["Color"], principled.inputs["Base Color"])
        elif file.endswith("_Roughness.png"):
            roughness_tex = nodes.new(type="ShaderNodeTexImage")
            roughness_tex.location = (-600, 0)
            roughness_tex.image = bpy.data.images.load(str(file_path))
            roughness_tex.image.colorspace_settings.name = "Non-Color"
            links.new(roughness_tex.outputs["Color"], principled.inputs["Roughness"])
        elif file.endswith("_NormalGL.png"):
            normal_tex = nodes.new(type="ShaderNodeTexImage")
            normal_tex.location = (-600, -300)
            normal_tex.image = bpy.data.images.load(str(file_path))
            normal_tex.image.colorspace_settings.name = "Non-Color"
            normal_map = nodes.new(type="ShaderNodeNormalMap")
            normal_map.location = (-300, -300)
            links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
            links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
        elif file.endswith("_Displacement.png"):
            displacement_tex = nodes.new(type="ShaderNodeTexImage")
            displacement_tex.location = (-600, -600)
            displacement_tex.image = bpy.data.images.load(str(file_path))
            displacement_tex.image.colorspace_settings.name = "Non-Color"
            displacement = nodes.new(type="ShaderNodeDisplacement")
            displacement.location = (-300, -600)
            links.new(displacement_tex.outputs["Color"], displacement.inputs["Height"])
            links.new(displacement.outputs["Displacement"], material_output.inputs["Displacement"])
    return material

def get_preview_icon(url):
    """
    Loads the image from the given URL into the preview collection (if not already loaded)
    and returns its icon_id so it can be used with template_icon.
    """
    pcoll = preview_collections["ambientcg"]
    # Use the URL as the key name for caching
    if url in pcoll:
        return pcoll[url].icon_id
    else:
        try:
            image_name = os.path.basename(url)
            image_path = os.path.join(get_cache_dir(), image_name)
            # Download if necessary
            if not os.path.exists(image_path):
                img_data = requests.get(url).content
                with open(image_path, 'wb') as handler:
                    handler.write(img_data)
            # Load the image into the preview collection
            preview = pcoll.load(url, image_path, 'IMAGE')
            return preview.icon_id
        except Exception as e:
            print(f"Failed to load preview image from URL {url}: {e}")
            return 0

# -------------------------------------------------------------------
# Modal download operator with progress
# -------------------------------------------------------------------
class ASSET_OT_Download(bpy.types.Operator):
    bl_idname = "asset.download"
    bl_label = "Download Asset"
    
    asset_id: bpy.props.StringProperty()
    
    # Internal variables for modal download
    _timer = None
    _generator = None
    _file = None
    _total_size = 0
    _downloaded = 0
    _zip_path = ""
    
    def execute(self, context):
        asset_name = self.asset_id
        resolution = context.scene.ambientcg_resolution
        cache_dir = get_cache_dir()
        extract_path = cache_dir / f"{asset_name}_{resolution}"
        if extract_path.exists():
            # Already downloaded; create material immediately.
            create_material_from_extracted(extract_path, asset_name)
            downloaded_assets[asset_name] = True
            self.report({"INFO"}, f"Material '{asset_name}' created using preexisting assets!")
            return {"FINISHED"}
        # Start streaming download
        url = f"https://ambientcg.com/get?file={asset_name}_{resolution}-PNG.zip"
        context.scene.ambientcg_current_download = asset_name
        context.scene.ambientcg_download_progress = 0.0
        self._zip_path = str(cache_dir / f"{asset_name}_{resolution}.zip")
        try:
            r = requests.get(url, stream=True)
            self._total_size = int(r.headers.get('content-length', 0))
            self._downloaded = 0
            self._generator = r.iter_content(chunk_size=int(self._total_size/100))
            self._file = open(self._zip_path, 'wb')
        except Exception as e:
            self.report({"ERROR"}, f"Download error: {e}")
            return {"CANCELLED"}
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            try:
                chunk = next(self._generator)
            except StopIteration:
                chunk = None
            if chunk:
                self._file.write(chunk)
                self._downloaded += len(chunk)
                progress = self._downloaded / self._total_size if self._total_size else 1.0
                context.scene.ambientcg_download_progress = progress
                context.area.tag_redraw()
            else:
                self._file.close()
                wm = context.window_manager
                wm.event_timer_remove(self._timer)
                context.scene.ambientcg_current_download = ""
                context.scene.ambientcg_download_progress = 1.0
                # Extraction and material creation
                result = fetch_and_create_material(self.asset_id, context.scene.ambientcg_resolution)
                if isinstance(result, str) and "Failed" in result:
                    self.report({"ERROR"}, result)
                    return {"CANCELLED"}
                extract_path = result
                create_material_from_extracted(extract_path, self.asset_id)
                downloaded_assets[self.asset_id] = True
                self.report({"INFO"}, f"Material '{self.asset_id}' created successfully!")
                return {"FINISHED"}
        return {"RUNNING_MODAL"}

# -------------------------------------------------------------------
# Panel to display the asset browser
# -------------------------------------------------------------------
class ASSET_PT_Menu(bpy.types.Panel):
    bl_label = "AmbientCG Assets"
    bl_idname = "ASSET_PT_menu"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Assets'
    
    def draw(self, context):
        layout = self.layout
        # Create a grid that flows to fill rows/columns based on available space
        grid = layout.grid_flow(row_major=True, columns=0, even_columns=True, even_rows=True, align=True)
        
        for asset_id, asset_link, asset_img in assets:
            col = grid.column(align=True)
            box = col.box()
            
            # Thumbnail Preview
            icon_id = get_preview_icon(asset_img)
            if icon_id:
                # Use a scale that fits well within the grid cell
                box.template_icon(icon_value=icon_id, scale=5)
            else:
                box.label(text="Preview Not Available")
            
            # Asset Name and Learn More Button
            row = box.row()
            learn_op = row.operator("url.open", text=asset_id, icon='INFO')
            learn_op.url = f"https://ambientcg.com{asset_link}"
            
            # Resolution Dropdown
            row = box.row()
            row.prop(context.scene, "ambientcg_resolution", text="Resolution")
            
            # Download Button/Status
            row = box.row()
            if asset_id in downloaded_assets:
                row.label(text="Downloaded", icon='CHECKMARK')
            elif context.scene.ambientcg_current_download == asset_id:
                row.label(text=f"Downloading: {int(context.scene.ambientcg_download_progress * 100)}%", icon='IMPORT')
            else:
                download_op = row.operator("asset.download", text="Download", icon='IMPORT')
                download_op.asset_id = asset_id

# -------------------------------------------------------------------
# Registration
# -------------------------------------------------------------------
classes = [URL_OT_Open, ASSET_OT_Download, ASSET_PT_Menu]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ambientcg_resolution = bpy.props.EnumProperty(
        name="Resolution",
        description="Resolution of the material textures",
        items=[
            ("1K", "1K", "1K resolution"),
            ("2K", "2K", "2K resolution"),
            ("4K", "4K", "4K resolution"),
            ("8K", "8K", "8K resolution"),
        ],
        default="1K",
    )
    bpy.types.Scene.ambientcg_current_download = bpy.props.StringProperty(default="")
    bpy.types.Scene.ambientcg_download_progress = bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    
    # Create our preview collection for thumbnails
    global preview_collections
    pcoll = bpy.utils.previews.new()
    preview_collections["ambientcg"] = pcoll

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.ambientcg_resolution
    del bpy.types.Scene.ambientcg_current_download
    del bpy.types.Scene.ambientcg_download_progress
    
    # Remove our preview collection
    global preview_collections
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()

if __name__ == "__main__":
    register()
