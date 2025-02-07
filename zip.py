import os
import zipfile

def zip_folder(folder_name):
    cwd = os.path.dirname(os.path.realpath(__file__))
    folder_path = os.path.join(cwd, folder_name)
    if not os.path.isdir(folder_path):
        print(f"The folder '{folder_name}' does not exist.")
        return

    zip_file_path = os.path.join(cwd, f"{folder_name}.zip")

    with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, cwd))
    
    print(f"Folder '{folder_name}' zipped successfully as '{zip_file_path}'.")

zip_folder('ambientcg-addon')