"""investor mode tables

Adds the four Investor Mode tables:

    investor_rebalance_log           one row per monthly rebalance
    investor_targets                 target weight per symbol per rebalance
    investor_news_events             every classified headline (incl. proxies)
    investor_fundamental_snapshots   point-in-time fundamentals + filing date

The pre-existing swing tables are untouched — Investor Mode keeps its own state
because the swing engine's single-position risk columns (initial_risk, heat
budgets, time stops) have no meaning in a 25-name monthly-rebalanced book.

Revision ID: 0001_investor_mode
Revises:
Create Date: 2026-10-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_investor_mode"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(table: str) -> bool:
    """True if the table is already present.

    This repo builds its legacy schema with `Base.metadata.create_all` at
    runtime rather than through migrations, so anyone who has run the app (or
    the test suite) against their working database already has these four
    tables. Without this guard `alembic upgrade head` dies with "table already
    exists" for exactly those developers.
    """
    return sa.inspect(op.get_bind()).has_table(table)


def _create_rebalance_log() -> None:
    op.create_table(
        "investor_rebalance_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rebalance_date", sa.Date(), nullable=False),
        sa.Column("regime", sa.String(length=20), nullable=True),
        sa.Column("exposure", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("equity", sa.Numeric(precision=16, scale=2), nullable=True),
        sa.Column("n_positions", sa.Integer(), nullable=True),
        sa.Column("target_weights", sa.JSON(), nullable=True),
        sa.Column("defensive_weights", sa.JSON(), nullable=True),
        sa.Column("eligible", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_investor_rebal_date", "investor_rebalance_log", ["rebalance_date"])


def _create_targets() -> None:
    op.create_table(
        "investor_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("target_weight", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("fundamental_score", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("is_defensive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["symbol"], ["universe.symbol"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_investor_target_date", "investor_targets", ["as_of"])
    op.create_index("idx_investor_target_symbol", "investor_targets", ["symbol", "as_of"])


def _create_news_events() -> None:
    op.create_table(
        "investor_news_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("subtype", sa.String(length=60), nullable=True),
        sa.Column("materiality", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("sentiment", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("finbert_score", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("tier", sa.String(length=10), nullable=True),
        sa.Column("is_macro", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_proxy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["symbol"], ["universe.symbol"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_news_date_symbol", "investor_news_events", ["trade_date", "symbol"])
    op.create_index("idx_news_type", "investor_news_events", ["event_type", "trade_date"])
    op.create_index("idx_news_proxy", "investor_news_events", ["is_proxy"])


def _create_fundamental_snapshots() -> None:
    op.create_table(
        "investor_fundamental_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=True),
        sa.Column("usable_from", sa.Date(), nullable=False),
        sa.Column("frequency", sa.String(length=20), nullable=True),
        sa.Column("roce", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("roe", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("debt_to_equity", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("interest_coverage", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("ocf_to_pat", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("promoter_pledge_pct", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("revenue_cagr_3y", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("eps_cagr_3y", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("loss_years_3y", sa.Integer(), nullable=True),
        sa.Column("pe", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("pb", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("composite_score", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("data_source", sa.String(length=60), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["symbol"], ["universe.symbol"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_invfund_symbol_period",
        "investor_fundamental_snapshots", ["symbol", "period_end"],
    )
    op.create_index(
        "idx_invfund_usable", "investor_fundamental_snapshots", ["usable_from"],
    )


def upgrade() -> None:
    if not _exists("investor_rebalance_log"):
        _create_rebalance_log()
    if not _exists("investor_targets"):
        _create_targets()
    if not _exists("investor_news_events"):
        _create_news_events()
    if not _exists("investor_fundamental_snapshots"):
        _create_fundamental_snapshots()


def downgrade() -> None:
    for table, indexes in (
        ("investor_fundamental_snapshots", ["idx_invfund_usable", "idx_invfund_symbol_period"]),
        ("investor_news_events", ["idx_news_proxy", "idx_news_type", "idx_news_date_symbol"]),
        ("investor_targets", ["idx_investor_target_symbol", "idx_investor_target_date"]),
        ("investor_rebalance_log", ["idx_investor_rebal_date"]),
    ):
        if not _exists(table):
            continue
        for idx in indexes:
            op.drop_index(idx, table_name=table)
        op.drop_table(table)