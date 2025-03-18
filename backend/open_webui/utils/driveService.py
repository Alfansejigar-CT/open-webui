import hashlib
import os
import io
import uuid
import base64
import re
import traceback
from datetime import datetime
from mimetypes import guess_type
from typing import Optional
from fastapi import logger
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials
from open_webui.models.externalResources import ExternalResource, ExternalResources
from open_webui.storage.provider import Storage
from open_webui.models.files import FileForm, Files
from open_webui.routers.retrieval import process_file, ProcessFileForm
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "Client_secret.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]
def create_service():
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build("drive", "v3", credentials=creds)
        return service
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to initialize Google Drive service: {str(e)}")
# def extract_metadata(raw_metadata: dict) -> dict:
#     extracted_metadata = {
#         "name": raw_metadata.get("name"),
#         "mimeType": raw_metadata.get("mimeType"),
#     }
#     return extracted_metadata
def fetch_file_metadata(service, drive_Id: str):
    try:
        file_metadata = service.files().get(fileId=drive_Id, fields="id, name, mimeType, parents").execute()
        return file_metadata
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Unable to fetch metadata for Drive ID {drive_Id}: {str(e)}")
def get_drive_files(drive_Id: str):
    try:
        service = create_service()
        metadata = fetch_file_metadata(service, drive_Id)
        files = []
        if metadata["mimeType"] == "application/vnd.google-apps.folder":
            query = f"'{drive_Id}' in parents and trashed=false"
            results = service.files().list(
                q=query,
                fields="files(id, name, mimeType, md5Checksum, modifiedTime, size, parents)"
            ).execute()
            drive_files = results.get("files", [])
        else:
            drive_files = [metadata]
        for file in drive_files:
            print(file["id"], "this is file id")  
            print(file["mimeType"], "this is file mimeType")  
            # Safely download the file
            downloaded_file = download_file(service, file)
            if downloaded_file:
                files.append(downloaded_file)
        return files
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Error retrieving files from Google Drive: {str(e)}")
    
def download_file(service, file):
    try:
        file_id = file.get("id")
        original_name = file.get("name")
        mime_type = file.get("mimeType")
        modified_time = file.get("modifiedTime")
        content_type = mime_type
        export_mime = None
        file_name = original_name
        # Handle Google Docs export
        if mime_type == "application/vnd.google-apps.document":
            export_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            base_name = os.path.splitext(original_name)[0]
            file_name = f"{base_name}.docx"  # Force .docx extension
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            content_type = export_mime  # Use correct MIME type
        elif mime_type in ["application/pdf", "image/png", "image/jpeg", "text/plain"]:
            request = service.files().get_media(fileId=file_id)
        else:
            print(f"Skipping unsupported file type: {mime_type}")
            return None
        # Download content
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        file_content = file_stream.getvalue()
        file_size = len(file_content)  # Get actual size from content
        return {
            "name": file_name,
            "fileId": file_id,
            "content": file_content,
            "meta": {
                "name": file_name,
                "content_type": content_type,  # Use corrected MIME type
                "size": file_size  # Actual content size
            },
            "md5": hashlib.md5(file_content).hexdigest(),  # Compute from actual content
            "modifiedTime": modified_time
        }
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Download failed: {str(e)}")
    

#########################################
async def update_drive_file(request, file_data, existing_file,user ):
    """
    Updates an existing file both in the storage system and the database.
    Args:
        file_data (dict): The updated file data from Google Drive.
        existing_file (FileModel): The current file record from the database.
        external_resource_id (int): The ID of the external resource associated with the file.
    Returns:
        FileModel: The updated file record from the database.
    """
    try:
        # Validate input data
        file_id = file_data.get("fileId")
        name = file_data.get("name")
        content = file_data.get("content")
        meta = file_data.get("meta", {})
        if not name or not content or not meta:
            raise ValueError("Missing required file data (name, content, or meta).")
        # Validate metadata fields
        if not meta.get("content_type"):
            raise ValueError(f"Missing 'content_type' in metadata for file '{name}'")
        if not meta.get("size"):
            raise ValueError(f"Missing 'size' in metadata for file '{name}'")
        
        # Generate a new unique filename for the updated file in storage
        new_filename = f"{file_id}_{os.path.basename(name)}"
        
        # Upload the new file content to the storage provider
        file_blob = io.BytesIO(content)
        _, new_file_path = Storage.upload_file(file_blob, new_filename)
        
        # Prepare the updated metadata
        updated_meta = {
            "name": meta.get("name", name),
            "content_type": meta.get("content_type"),
            "size": int(meta.get("size")),
            "md5": file_data.get("md5"),  # MD5 checksum
            "modified_time": file_data.get("modifiedTime"),
        }
        
        # Update the existing database record
        Files.update_file_metadata_by_id(existing_file.id, updated_meta)
        updated_file = Files.update_file_data_by_id(
            existing_file.id,
            {"path": new_file_path, "meta": updated_meta},
        )
        
        # Trigger optional post-update actions, like reprocessing the file
        process_file(request, ProcessFileForm(file_id=existing_file.id),user=user)
        
        return updated_file
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to update Google Drive file '{file_data.get('name', 'unknown')}': {str(e)}")

    ####################################################
    
async def upload_drive_file(request, file_data, user, external_resource_id):
    try:
        # Extract necessary file data
        name = file_data.get("name")
        content = file_data.get("content")
        meta = file_data.get("meta", {})
        file_id = file_data.get("fileId")
        # Validate essential fields in file_data and meta
        if not name or not content or not meta:
            raise ValueError("Missing required file data (name, content, or meta).")
        # Validate metadata fields: 'content_type' and 'size' are mandatory
        if not meta.get("content_type"):
            raise ValueError(f"Missing 'content_type' in metadata for file '{name}'")
        if not meta.get("size"):
            raise ValueError(f"Missing 'size' in metadata for file '{name}'")
        # Extract additional metadata fields
        md5_checksum = file_data.get("md5")  # MD5 checksum (optional but recommended)
        modified_time = file_data.get("modifiedTime")  # Last modified time (optional)
        # Generate a unique filename and upload to storage
        filename = f"{file_id}_{os.path.basename(name)}"
        file_blob = io.BytesIO(content)
        contents, file_path = Storage.upload_file(file_blob, filename)
        # Construct `FileForm` metadata according to `FileMeta` structure
        form_data = FileForm(
            id=file_id,  # External Google Drive file ID
            filename=name,
            path=file_path,
            meta={
                "name": meta.get("name", name),  # File name falls back to `name`
                "content_type": meta.get("content_type"),  # MIME type
                "size": int(meta.get("size", 0)),  # File size in bytes
                "md5": md5_checksum,  # MD5 checksum
                "modified_time": modified_time  # Last modified timestamp
            },
            data={},
            access_control=None,  # Leave unset for now
            external_resource_id=external_resource_id,
        )
        # Insert the file into the database
        file_item = Files.insert_new_external_file(user_id=user.id, form_data=form_data)
        if not file_item:
            raise Exception(f"Database insertion failed for file '{name}'")
        # Optionally process the file after successful upload
        process_file(request, ProcessFileForm(file_id=file_item.id),user=user)
        return file_item
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to upload Google Drive file '{file_data.get('name', 'unknown')}': {str(e)}")
    
async def get_drive_Id(drive_link: str) -> str | None:
    pattern = (
        r"https?://(?:drive\.google\.com/(?:file/d/|drive/folders/|open\?id=|uc\?id=)|"
        r"docs\.google\.com/(?:document|spreadsheets|presentation)/d/)"
        r"([\w-]+)"
    )
    match = re.search(pattern, drive_link)
    return match.group(1) if match else None


async def process_google_drive_link(drive_link, user, request):
    user_id = user.id
    try:
        drive_id = await get_drive_Id(drive_link)
        if not drive_id:
            raise Exception("Invalid Google Drive link: Unable to extract drive ID.")
        resource = ExternalResources.insert_new_resource(
            user_id=user_id,
            resource_link=drive_link
        )
        if not resource:
            raise Exception("Failed to create an entry for the external resource.")
        service = create_service()
        start_token = service.changes().getStartPageToken().execute()
        if "startPageToken" not in start_token:
            raise Exception("Failed to retrieve startPageToken.")
        ExternalResources.update_last_sync_and_token(resource_id=resource.id, page_token=start_token["startPageToken"])
        raw_files = get_drive_files(drive_id)
        uploaded_files = []
        for file_data in raw_files:
            uploaded_file = await upload_drive_file(
                request,
                file_data,
                user,
                external_resource_id=resource.id  
            )
            uploaded_files.append(uploaded_file)
        return {
        "files_metadata": [{
            "id": f.id,
            "name": f.filename,
            "path": f.path,
            "meta": f.meta
        } for f in uploaded_files]
    }

    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to process Google Drive link: {str(e)}")
    

async def sync_single_drive_resource(request, resource: ExternalResource, user):
    """Simplified sync without batch processing."""
    try:
        service = create_service()
        
        # Validate resource input
        if not resource or not resource.resource_link or not resource.page_token:
            raise ValueError("Invalid resource object or missing required fields")
            
        # Extract the Google Drive ID
        drive_id = await get_drive_Id(resource.resource_link)
        if not drive_id:
            raise Exception("Invalid Google Drive link: Unable to extract drive ID.")
            
        # Fetch file metadata to determine if it is a folder
        metadata = fetch_file_metadata(service, drive_id)
        if not metadata:
            raise Exception(f"Unable to fetch metadata for drive ID: {drive_id}")
            
        is_folder = metadata["mimeType"] == "application/vnd.google-apps.folder"
        
        # Process Drive changes
        changes = service.changes().list(
            pageToken=resource.page_token,
            spaces="drive",
            fields="changes(file(id,name,mimeType,md5Checksum,modifiedTime,size,parents),removed,fileId),newStartPageToken"
        ).execute()
        
        updated_files_metadata = []
        
        for change in changes.get("changes", []):
            file_id = change.get("fileId")
            file_data = change.get("file", {})  # This contains metadata of the file
            
            # Skip changes not related to the target file/folder
            if is_folder and (not file_data.get("parents") or drive_id not in file_data.get("parents")):
                continue
            elif not is_folder and file_id != drive_id:
                continue
                
            try:
                if change.get("removed") or file_data.get("trashed"):
                    # Handle file deletion
                    existing_file = Files.get_file_by_external_file_id(file_id)
                    if existing_file:
                        # Perform file deletion from storage
                        if existing_file.path:
                            try:
                                Storage.delete_file(existing_file.path)
                            except Exception as storage_error:
                                logger.error(f"Error removing file from storage: {existing_file.path}, Error: {storage_error}")
                                
                        # Delete from database
                        Files.delete_file_by_id(existing_file.id)
                        
                        # Append metadata for deleted file
                        updated_files_metadata.append({
                            "id": existing_file.id,
                            "name": existing_file.filename,
                            "path": existing_file.path,
                            "meta": existing_file.meta
                        })
                else:
                    # For files that weren't removed, we need to download the content
                    # before we can update or create them
                    downloaded_file = download_file(service, file_data)
                    if not downloaded_file:
                        logger.warning(f"Skipping file {file_id}: Unable to download content")
                        continue
                        
                    # Now file_data has content and complete metadata
                    file_data = downloaded_file
                    
                    existing_file = Files.get_file_by_external_file_id(file_id)
                    
                    # Determine if update is needed based on file type
                    if existing_file:
                        is_google_native = file_data.get("mimeType", "").startswith("application/vnd.google-apps.")
                        needs_update = (
                            existing_file.modified_time != file_data.get("modifiedTime") 
                            if is_google_native
                            else existing_file.md5_hash != file_data.get("md5")
                        )
                        
                        if needs_update:
                            # Update the file
                            updated_file = await update_drive_file(request, file_data, existing_file, user)
                            updated_files_metadata.append({
                                "id": updated_file.id,
                                "name": updated_file.filename,
                                "path": updated_file.path if hasattr(updated_file, "path") else updated_file.get("path"),
                                "meta": updated_file.meta
                            })
                    else:
                        # Create a new file if it doesn't exist
                        new_file = await upload_drive_file(request, file_data, user, resource.id)
                        updated_files_metadata.append({
                            "id": new_file.id,
                            "name": new_file.filename,
                            "path": new_file.path,
                            "meta": new_file.meta
                        })
            except Exception as e:
                logger.error(f"Error processing change for file {file_id}: {str(e)}", exc_info=True)
                
        # Update the page token after successful sync
        ExternalResources.update_last_sync_and_token(
            resource_id=resource.id,
            page_token=changes.get("newStartPageToken")
        )
        
        # Return a consistent metadata structure (similar to `process_google_drive_link`)
        return {
            "files_metadata": updated_files_metadata
        }
    except Exception as e:
        logger.error(f"Error syncing Drive resource {resource.id}: {e}", exc_info=True)
        raise