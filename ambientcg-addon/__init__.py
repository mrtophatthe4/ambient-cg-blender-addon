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
import threading
from pathlib import Path
import bpy.utils.previews

# Global dictionaries and locks for preview downloading
preview_download_threads = {}
preview_download_lock = threading.Lock()

# -------------------------------------------------------------------
# Initial Asset Fetching (filtering out HDRIs and Substance materials)
# -------------------------------------------------------------------
assets = []
ASSET_LIST_URL = (
    "https://ambientcg.com/hx/asset-list?id=&childrenOf=&variationsOf=&parentsOf="
    "&q=ball&colorMode=&thumbnails=200&sort=popular"
)
response = requests.get(ASSET_LIST_URL)
asset_pattern = re.compile(r'<div class="asset-block" id="asset-([^"]+)">')
link_pattern  = re.compile(r'<a\s+href="(/view\?id=[^"]+)">')
img_pattern   = re.compile(r'<img[^>]+class="only-show-dark"[^>]+src="([^"]+)"')
for match in zip(asset_pattern.finditer(response.text),
                 link_pattern.finditer(response.text),
                 img_pattern.finditer(response.text)):
    asset_id = match[0].group(1)
    # Skip HDRIs and Substance materials
    if "hdri" in asset_id.lower() or "substance" in asset_id.lower():
        continue
    asset_link = match[1].group(1)
    asset_img = match[2].group(1)
    assets.append((asset_id, asset_link, asset_img))

downloaded_assets = {}
preload_queue = []
preload_operator_running = False
search_query = ""
original_assets = assets.copy()
current_page = 1
total_pages = 1
items_per_page = 20
preview_collections = {}

# -------------------------------------------------------------------
# Custom URL operator
# -------------------------------------------------------------------
class URL_OT_Open(bpy.types.Operator):
    bl_idname = "url.open"
    bl_label = "Open URL"
    
    url: bpy.props.StringProperty(default="")

    def execute(self, context):
        webbrowser.open(self.url)
        return {'FINISHED'}

# -------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------
def get_cache_dir():
    home = Path.home()
    cache_dir = home / ".cache" / "ambientcg"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

def fetch_and_create_material(material_name, resolution):
    url = f"https://ambientcg.com/get?file={material_name}_{resolution}-PNG.zip"
    cache_dir = get_cache_dir()
    extract_path = cache_dir / f"{material_name}_{resolution}"
    zip_path = cache_dir / f"{material_name}_{resolution}.zip"
    if not extract_path.exists():
        if not zip_path.exists():
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
            zip_path.unlink()
        except Exception as e:
            return f"Failed to extract zip file: {str(e)}"
    return extract_path

def create_material_from_extracted(extract_path, asset_name):
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

def download_preview_async(url):
    image_name = os.path.basename(url)
    image_path = os.path.join(get_cache_dir(), image_name)
    try:
        img_data = requests.get(url).content
        with open(image_path, 'wb') as handler:
            handler.write(img_data)
    except Exception as e:
        print(f"Failed to download preview image from URL {url}: {e}")
    with preview_download_lock:
        if url in preview_download_threads:
            del preview_download_threads[url]

def get_preview_icon(url):
    pcoll = preview_collections["ambientcg"]
    if url in pcoll:
        return pcoll[url].icon_id
    else:
        image_name = os.path.basename(url)
        image_path = os.path.join(get_cache_dir(), image_name)
        if not os.path.exists(image_path):
            with preview_download_lock:
                if url not in preview_download_threads:
                    t = threading.Thread(target=download_preview_async, args=(url,))
                    t.daemon = True
                    t.start()
                    preview_download_threads[url] = t
            return 0
        try:
            preview = pcoll.load(url, image_path, 'IMAGE')
            return preview.icon_id
        except Exception as e:
            print(f"Failed to load preview image from URL {url}: {e}")
            return 0

# -------------------------------------------------------------------
# Async download operator with progress and material application
# -------------------------------------------------------------------
class ASSET_OT_Download(bpy.types.Operator):
    bl_idname = "asset.download"
    bl_label = "Download Asset"
    
    asset_id: bpy.props.StringProperty()
    
    _timer = None
    _download_thread = None
    _total_size = 0
    _downloaded = 0
    _zip_path = ""
    _download_finished = False
    _download_error = None

    def execute(self, context):
        asset_name = self.asset_id
        resolution = context.scene.ambientcg_resolution
        cache_dir = get_cache_dir()
        extract_path = cache_dir / f"{asset_name}_{resolution}"
        if extract_path.exists():
            mat = create_material_from_extracted(extract_path, asset_name)
            downloaded_assets[asset_name] = True
            obj = context.active_object
            if obj and len(obj.material_slots) == 1:
                obj.material_slots[0].material = mat
            self.report({"INFO"}, f"Material '{asset_name}' created using preexisting assets!")
            return {"FINISHED"}
        url = f"https://ambientcg.com/get?file={asset_name}_{resolution}-PNG.zip"
        context.scene.ambientcg_current_download = asset_name
        context.scene.ambientcg_download_progress = 0.0
        self._zip_path = str(cache_dir / f"{asset_name}_{resolution}.zip")
        self._downloaded = 0
        self._total_size = 0
        self._download_finished = False
        self._download_error = None
        self._download_thread = threading.Thread(target=self.download_thread, args=(url,))
        self._download_thread.start()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}
    
    def download_thread(self, url):
        try:
            r = requests.get(url, stream=True)
            self._total_size = int(r.headers.get('content-length', 0))
            with open(self._zip_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
                        self._downloaded += len(chunk)
            self._download_finished = True
        except Exception as e:
            self._download_error = str(e)
            self._download_finished = True

    def modal(self, context, event):
        if event.type == 'TIMER':
            if self._total_size:
                progress = self._downloaded / self._total_size
            else:
                progress = 0.0
            context.scene.ambientcg_download_progress = progress
            context.area.tag_redraw()
            if self._download_finished:
                wm = context.window_manager
                wm.event_timer_remove(self._timer)
                if self._download_error:
                    self.report({"ERROR"}, f"Download error: {self._download_error}")
                    return {"CANCELLED"}
                context.scene.ambientcg_download_progress = 1.0
                result = fetch_and_create_material(self.asset_id, context.scene.ambientcg_resolution)
                if isinstance(result, str) and "Failed" in result:
                    self.report({"ERROR"}, result)
                    return {"CANCELLED"}
                extract_path = result
                mat = create_material_from_extracted(extract_path, self.asset_id)
                downloaded_assets[self.asset_id] = True
                obj = context.active_object
                if obj and len(obj.material_slots) == 1:
                    obj.material_slots[0].material = mat
                self.report({"INFO"}, f"Material '{self.asset_id}' created successfully!")
                return {"FINISHED"}
        return {"RUNNING_MODAL"}

# -------------------------------------------------------------------
# Thumbnail loading system
# -------------------------------------------------------------------
def preload_thumbnails():
    global preload_queue, preload_operator_running
    if not preload_operator_running and assets:
        preload_queue = [asset[2] for asset in assets]
        preload_operator_running = True
        bpy.app.timers.register(load_next_thumbnail, first_interval=0.1)

def load_next_thumbnail():
    global preload_queue, preload_operator_running
    if not preload_queue:
        preload_operator_running = False
        return None
    url = preload_queue.pop(0)
    pcoll = preview_collections["ambientcg"]
    if url not in pcoll:
        get_preview_icon(url)
    # Force a redraw periodically
    if len(preload_queue) % 5 == 0:
        for area in bpy.context.window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return 0.01 if preload_queue else None

# -------------------------------------------------------------------
# Search-related functions
# -------------------------------------------------------------------
def update_asset_search(query):
    global assets, search_query, preload_queue, preload_operator_running
    search_query = query.strip().lower()
    if not search_query:
        assets = original_assets.copy()
    else:
        filtered = []
        for asset in original_assets:
            if search_query in asset[0].lower():
                filtered.append(asset)
        assets = filtered
    # Clear out the preview collection so new thumbnails are loaded
    if "ambientcg" in preview_collections:
        preview_collections["ambientcg"].clear()
    preload_queue = [asset[2] for asset in assets]
    preload_operator_running = False
    bpy.app.timers.register(load_next_thumbnail, first_interval=0.1)

# -------------------------------------------------------------------
# Panel with Search Bar and Pagination
# -------------------------------------------------------------------
class ASSET_PT_Menu(bpy.types.Panel):
    bl_label = "AmbientCG Assets"
    bl_idname = "ASSET_PT_menu"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Assets'
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column()
        row = col.row(align=True)
        row.prop(scene, "ambientcg_search_query", text="", icon='VIEWZOOM')
        row.operator("ambientcg.search", text="", icon='FILE_REFRESH').direction = ""
        
        # pagination_row = col.row(align=True)
        # if current_page > 1:
        #     prev_op = pagination_row.operator("ambientcg.search", text="", icon='TRIA_LEFT', emboss=True)
        #     prev_op.direction = "prev"
        # else:
        #     pagination_row.label(text="", icon='BLANK1')
        # pagination_row.label(text=f"Page {current_page} of {total_pages}")
        # if current_page < total_pages:
        #     next_op = pagination_row.operator("ambientcg.search", text="", icon='TRIA_RIGHT', emboss=True)
        #     next_op.direction = "next"
        # else:
        #     pagination_row.label(text="", icon='BLANK1')
        
        if search_query:
            layout.label(text=f"Showing results for: '{search_query}'", icon='FILTER')
        
        grid = layout.grid_flow(row_major=True, columns=0, even_columns=True, even_rows=True, align=True)
        if not preload_operator_running:
            preload_thumbnails()
        if not assets:
            layout.label(text="No assets found matching your search", icon='INFO')
            return
        for asset_id, asset_link, asset_img in assets:
            col = grid.column(align=True)
            box = col.box()
            pcoll = preview_collections["ambientcg"]
            preview_loaded = asset_img in pcoll
            if preview_loaded:
                icon_id = pcoll[asset_img].icon_id
                box.template_icon(icon_value=icon_id, scale=5)
            else:
                box.label(text="Loading Preview...", icon='IMAGE_DATA')
            row = box.row()
            learn_op = row.operator("url.open", text=asset_id, icon='INFO')
            learn_op.url = f"https://ambientcg.com{asset_link}"
            row = box.row()
            row.prop(scene, "ambientcg_resolution", text="Resolution")
            row = box.row()
            if asset_id in downloaded_assets:
                row.label(text="Downloaded", icon='CHECKMARK')
            elif scene.ambientcg_current_download == asset_id:
                row.label(text=f"Downloading: {int(scene.ambientcg_download_progress * 100)}%", icon='IMPORT')
            else:
                download_op = row.operator("asset.download", text="Download", icon='IMPORT')
                download_op.asset_id = asset_id

# -------------------------------------------------------------------
# Search Operator with Pagination
# -------------------------------------------------------------------
class AMBIENTCG_OT_Search(bpy.types.Operator):
    bl_idname = "ambientcg.search"
    bl_label = "Search AmbientCG Assets"
    
    direction: bpy.props.StringProperty(default="")
    
    def execute(self, context):
        global current_page, total_pages
        scene = context.scene
        if self.direction == "next":
            current_page += 1
        elif self.direction == "prev":
            current_page = max(1, current_page - 1)
        else:
            current_page = 1
        offset = (current_page - 1) * items_per_page
        ASSET_LIST_URL = (
            f"https://ambientcg.com/hx/asset-list?"
            f"q={scene.ambientcg_search_query}"
            f"&colorMode=&thumbnails=200&sort=popular"
            f"&offset={offset}&count={items_per_page}"
        )
        try:
            response = requests.get(ASSET_LIST_URL)
            asset_pattern = re.compile(r'<div class="asset-block" id="asset-([^"]+)">')
            link_pattern = re.compile(r'<a\s+href="(/view\?id=[^"]+)">')
            img_pattern = re.compile(r'<img[^>]+class="only-show-dark"[^>]+src="([^"]+)"')
            total_pattern = re.compile(r'Showing \d+ - \d+ of (\d+) results')
            total_match = total_pattern.search(response.text)
            total_results = int(total_match.group(1)) if total_match else 0
            total_pages = max(1, (total_results + items_per_page - 1) // items_per_page)
            global assets, original_assets
            assets = []
            for match in zip(asset_pattern.finditer(response.text),
                             link_pattern.finditer(response.text),
                             img_pattern.finditer(response.text)):
                asset_id = match[0].group(1)
                # Filter out HDRIs and Substance materials
                if "hdri" in asset_id.lower() or "substance" in asset_id.lower():
                    continue
                asset_link = match[1].group(1)
                asset_img = match[2].group(1)
                assets.append((asset_id, asset_link, asset_img))
            original_assets = assets.copy()
            update_asset_search(scene.ambientcg_search_query)
        except Exception as e:
            self.report({'ERROR'}, f"Search failed: {str(e)}")
        return {'FINISHED'}

# -------------------------------------------------------------------
# Registration
# -------------------------------------------------------------------
classes = [URL_OT_Open, ASSET_OT_Download, ASSET_PT_Menu, AMBIENTCG_OT_Search]

def register():
    bpy.types.Scene.ambientcg_search_query = bpy.props.StringProperty(
        name="Search",
        description="Search for AmbientCG assets",
        default=""
    )
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
    global preview_collections
    pcoll = bpy.utils.previews.new()
    preview_collections["ambientcg"] = pcoll

def unregister():
    global preload_queue, preload_operator_running
    preload_queue = []
    preload_operator_running = False
    if bpy.app.timers.is_registered(load_next_thumbnail):
        bpy.app.timers.unregister(load_next_thumbnail)
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.ambientcg_search_query
    del bpy.types.Scene.ambientcg_resolution
    del bpy.types.Scene.ambientcg_current_download
    del bpy.types.Scene.ambientcg_download_progress
    global preview_collections
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()

if __name__ == "__main__":
    register()
