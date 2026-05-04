from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import settings
from app.database import Base

# Import all models so Alembic detects them
from app.modules.platform.models import User, UserRole, Session, EnabledLanguage
from app.modules.auth.models import PhoneOTP
from app.modules.clients.models import (
    Client, ClientOrganisationType, ClientUser, ClientLocation,
    ClientCrop, CropExpertAssignment, CMClientAssignment, CMPrivilegeModel
)
from app.modules.sync.models import CoshSyncLog, CoshReferenceCache, VolumeFormula, CropHealthCrop
from app.modules.orders.models import (
    Order, OrderItem, SeedOrder, PackingList, MissingBrandReport
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionWaitlist, SubscriptionPool,
    AlertRecipient, Alert, PromoterAssignment,
    FarmerSubscriptionHistory, SubscriptionPaymentRequest,
)
from app.modules.subscriptions.snapshot_models import LockedTimelineSnapshot
from app.modules.subscriptions.config_error_models import DataConfigError
from app.modules.subscriptions.promoter_allocation_models import PromoterAllocation
from app.modules.advisory.models import (
    Package, PackageLocation, PackageAuthor, Parameter, ParameterTranslation,
    Variable, VariableTranslation, PackageVariable, Timeline, Practice, Element,
    Relation, ConditionalQuestion, ConditionalQuestionTranslation, PracticeConditional,
    PGRecommendation, PGTimeline, PGPractice, PGElement,
    SPRecommendation, SPTimeline, SPPractice, SPElement,
)
from app.modules.qr.models import ManufacturerBrandPortfolio, ProductQRCode, QRScan
from app.modules.farmpundit.models import (
    FarmPunditProfile, FarmPunditExpertise, FarmPunditSupportArea,
    ClientFarmPundit, PunditInvitation, Query, QueryMedia,
    QueryRemark, QueryResponse, StandardResponse,
)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
