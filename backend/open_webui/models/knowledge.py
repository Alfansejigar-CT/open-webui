import json
import logging
import time
from typing import Optional
import uuid

from open_webui.internal.db import Base, get_db
from open_webui.env import SRC_LOG_LEVELS

from open_webui.models.files import FileMetadataResponse
from open_webui.models.users import Users, UserResponse


from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, Text, JSON

from open_webui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# Knowledge DB Schema
####################


class Knowledge(Base):
    __tablename__ = "knowledge"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text)

    data = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)

    access_control = Column(JSON, nullable=True)  # Controls data access levels.
    # Defines access control rules for this entry.
    # - `None`: Public access, available to all users with the "user" role.
    # - `{}`: Private access, restricted exclusively to the owner.
    # - Custom permissions: Specific access control for reading and writing;
    #   Can specify group or user-level restrictions:
    #   {
    #      "read": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      },
    #      "write": {
    #          "group_ids": ["group_id1", "group_id2"],
    #          "user_ids":  ["user_id1", "user_id2"]
    #      }
    #   }

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str

    data: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


####################
# Forms
####################


class KnowledgeUserModel(KnowledgeModel):
    user: Optional[UserResponse] = None


class KnowledgeResponse(KnowledgeModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class KnowledgeUserResponse(KnowledgeUserModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class KnowledgeForm(BaseModel):
    name: str
    description: str
    data: Optional[dict] = None
    access_control: Optional[dict] = None


class KnowledgeTable:
    def insert_new_knowledge(
        self, user_id: str, form_data: KnowledgeForm
    ) -> Optional[KnowledgeModel]:
        with get_db() as db:
            knowledge = KnowledgeModel(
                **{
                    **form_data.model_dump(),
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            try:
                result = Knowledge(**knowledge.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                if result:
                    return KnowledgeModel.model_validate(result)
                else:
                    return None
            except Exception:
                return None

    def get_knowledge_bases(self) -> list[KnowledgeUserModel]:
        with get_db() as db:
            knowledge_bases = []
            for knowledge in (
                db.query(Knowledge).order_by(Knowledge.updated_at.desc()).all()
            ):
                user = Users.get_user_by_id(knowledge.user_id)
                knowledge_bases.append(
                    KnowledgeUserModel.model_validate(
                        {
                            **KnowledgeModel.model_validate(knowledge).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return knowledge_bases

    def get_knowledge_bases_by_user_id(
        self, user_id: str, permission: str = "write"
    ) -> list[KnowledgeUserModel]:
        knowledge_bases = self.get_knowledge_bases()
        return [
            knowledge_base
            for knowledge_base in knowledge_bases
            if knowledge_base.user_id == user_id
            or has_access(user_id, permission, knowledge_base.access_control)
        ]

    def get_knowledge_by_id(self, id: str) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = db.query(Knowledge).filter_by(id=id).first()
                return KnowledgeModel.model_validate(knowledge) if knowledge else None
        except Exception:
            return None

    def update_knowledge_by_id(
        self, id: str, form_data: KnowledgeForm, overwrite: bool = False
    ) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = self.get_knowledge_by_id(id=id)
                db.query(Knowledge).filter_by(id=id).update(
                    {
                        **form_data.model_dump(),
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_knowledge_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def update_knowledge_data_by_id(
        self, id: str, data: dict
    ) -> Optional[KnowledgeModel]:
        try:
            with get_db() as db:
                knowledge = self.get_knowledge_by_id(id=id)
                db.query(Knowledge).filter_by(id=id).update(
                    {
                        "data": data,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_knowledge_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete_knowledge_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Knowledge).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def delete_all_knowledge(self) -> bool:
        with get_db() as db:
            try:
                db.query(Knowledge).delete()
                db.commit()

                return True
            except Exception:
                return False
            

    def add_external_link_to_knowledge(self, id: str, external_link: str):
        """
        Add an external link to the `external_links` array inside the knowledge base's `data` field.
        """
        with get_db() as db:
            knowledge = db.query(Knowledge).filter_by(id=id).first()
            
            if not knowledge:
                raise ValueError("Knowledge base not found.")

            # Get the current data field or initialize it
            data = knowledge.data or {}
            external_links = data.get("external_links", [])

            if external_link in external_links:
                raise ValueError("External link already exists in the knowledge base.")

            # Append the new link to the list
            external_links.append(external_link)
            data["external_links"] = external_links

            # Perform the update
            db.query(Knowledge).filter_by(id=id).update(
                {"data": data, "updated_at": int(time.time())}
            )
            db.commit()

            # Return the updated knowledge base
            return KnowledgeModel.model_validate(knowledge)
    



    def get_knowledge_by_user_and_link(self, user_id: str, external_link: str) -> Optional[KnowledgeModel]:
        """
        Fetch a knowledge base by user ID and an external link in its `external_links`.

        Args:
            user_id: The ID of the user who owns the knowledge base.
            external_link: The external link to match in the `external_links` array.

        Returns:
            KnowledgeModel: The knowledge base that matches the criteria, or None if not found.
        """
        try:
            with get_db() as db:
                # Query for knowledge bases by user_id and filter matching the external link
                knowledge = db.query(Knowledge).filter_by(user_id=user_id).all()

                # Iterate over the results to match the external link in the `data.external_links`
                for kb in knowledge:
                    data = kb.data or {}
                    external_links = data.get("external_links", [])
                    if external_link in external_links:
                        return KnowledgeModel.model_validate(kb)
                
                # If no matching knowledge base is found, return None
                return None
        
        except Exception as e:
            log.exception("Error retrieving knowledge by user ID and external link.")
            return None

Knowledges = KnowledgeTable()
