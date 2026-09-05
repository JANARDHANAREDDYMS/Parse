from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("slug <> ''", name="slug_not_empty"),
        CheckConstraint("name <> ''", name="name_not_empty"),
    )

    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

