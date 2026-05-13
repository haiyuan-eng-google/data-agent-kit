"""Tests for AsyncBigQueryWriter."""

from unittest.mock import MagicMock, patch

import pytest

from ucp_analytics.writer import AsyncBigQueryWriter, get_ddl


class TestAsyncBigQueryWriter:
    @pytest.fixture
    def writer(self):
        return AsyncBigQueryWriter(
            project_id="test-project",
            dataset_id="test_dataset",
            table_id="test_table",
            batch_size=3,
            auto_create_table=False,
        )

    async def test_enqueue_buffers(self, writer):
        await writer.enqueue({"event_id": "1", "event_type": "test"})
        assert len(writer._buffer) == 1

    async def test_flush_when_batch_size_reached(self, writer):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []
        writer._client = mock_client
        writer.auto_create_table = False

        await writer.enqueue({"event_id": "1", "event_type": "test"})
        await writer.enqueue({"event_id": "2", "event_type": "test"})
        # Third enqueue triggers flush (batch_size=3)
        with patch("asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)):
            await writer.enqueue({"event_id": "3", "event_type": "test"})

        mock_client.insert_rows_json.assert_called_once()
        assert len(writer._buffer) == 0

    async def test_flush_empty_noop(self, writer):
        # Should not raise
        await writer.flush()
        assert len(writer._buffer) == 0

    async def test_flush_requeues_on_error(self, writer):
        mock_client = MagicMock()
        mock_client.insert_rows_json.side_effect = Exception("BQ down")
        writer._client = mock_client

        await writer.enqueue({"event_id": "1", "event_type": "test"})

        with patch("asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)):
            await writer.flush()

        # Rows should be re-queued
        assert len(writer._buffer) == 1
        assert writer._buffer[0]["event_id"] == "1"

    async def test_max_buffer_size_drops_oldest(self):
        writer = AsyncBigQueryWriter(
            project_id="test",
            dataset_id="ds",
            batch_size=100,  # high so no auto-flush
            auto_create_table=False,
            max_buffer_size=3,
        )

        await writer.enqueue({"event_id": "1"})
        await writer.enqueue({"event_id": "2"})
        await writer.enqueue({"event_id": "3"})
        # Buffer full, next enqueue should drop oldest
        await writer.enqueue({"event_id": "4"})

        assert len(writer._buffer) == 3
        ids = [r["event_id"] for r in writer._buffer]
        assert ids == ["2", "3", "4"]

    async def test_flush_requeues_only_failed_rows(self, writer):
        """Row-level errors from insert_rows_json should requeue only failed rows."""
        mock_client = MagicMock()
        # Simulate row-level error on index 0 only; index 1 succeeded
        mock_client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "invalid"}]}
        ]
        writer._client = mock_client

        await writer.enqueue({"event_id": "1", "event_type": "test"})
        await writer.enqueue({"event_id": "2", "event_type": "test"})

        with patch("asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)):
            await writer.flush()

        # Only the failed row (index 0) should be re-queued
        assert len(writer._buffer) == 1
        assert writer._buffer[0]["event_id"] == "1"

    async def test_flush_requeues_multiple_failed_rows(self, writer):
        """Multiple row-level errors requeue only those specific rows."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "invalid"}]},
            {"index": 2, "errors": [{"reason": "schema"}]},
        ]
        writer._client = mock_client

        await writer.enqueue({"event_id": "1", "event_type": "test"})
        await writer.enqueue({"event_id": "2", "event_type": "test"})
        await writer.enqueue({"event_id": "3", "event_type": "test"})

        with patch("asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)):
            # batch_size=3, but we already have 3 so flush manually
            writer._buffer.clear()
            writer._buffer.extend(
                [
                    {"event_id": "1", "event_type": "test"},
                    {"event_id": "2", "event_type": "test"},
                    {"event_id": "3", "event_type": "test"},
                ]
            )
            await writer.flush()

        # Only rows at index 0 and 2 should be re-queued
        assert len(writer._buffer) == 2
        ids = [r["event_id"] for r in writer._buffer]
        assert ids == ["1", "3"]

    async def test_close_flushes(self, writer):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []
        mock_client.close = MagicMock()
        writer._client = mock_client

        await writer.enqueue({"event_id": "1", "event_type": "test"})

        with patch("asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)):
            await writer.close()

        mock_client.insert_rows_json.assert_called_once()
        mock_client.close.assert_called_once()

    def test_full_table_id(self, writer):
        assert writer.full_table_id == "test-project.test_dataset.test_table"


class TestGetDDL:
    def test_generates_valid_ddl(self):
        ddl = get_ddl("my-project", "my_dataset", "my_table")
        assert "CREATE TABLE IF NOT EXISTS" in ddl
        assert "`my-project.my_dataset.my_table`" in ddl
        assert "event_id STRING NOT NULL" in ddl
        assert "PARTITION BY DATE(timestamp)" in ddl
        assert "CLUSTER BY event_type" in ddl

    def test_integer_mapped_to_int64(self):
        ddl = get_ddl("p", "d", "t")
        assert "INT64" in ddl
        assert "FLOAT64" in ddl

    def test_eligibility_outcome_columns_present(self):
        """A5 — pin that all three eligibility outcome BOOL columns
        are emitted into the DDL. Missing one of these would silently
        drop the corresponding KPI on table creation; auto_create_table
        would then succeed but downstream INSERTs would fail with
        'no such column'."""
        ddl = get_ddl("p", "d", "t")
        assert "eligibility_accepted_present BOOL" in ddl
        assert "eligibility_not_accepted_present BOOL" in ddl
        assert "eligibility_invalid_present BOOL" in ddl

    def test_totals_and_order_label_columns_present(self):
        """B1/C9/C7 finisher — pin the three new columns in the DDL."""
        ddl = get_ddl("p", "d", "t")
        assert "totals_json JSON" in ddl
        assert "order_label STRING" in ddl

    def test_signals_columns_present(self):
        """A6 — pin all three signals columns in the DDL."""
        ddl = get_ddl("p", "d", "t")
        assert "signals_present BOOL" in ddl
        assert "signals_keys_json JSON" in ddl
        assert "signals_json JSON" in ddl

    def test_ap2_mandate_columns_present(self):
        """A4 — pin all five AP2 columns. Missing one would silently
        drop the corresponding KPI on table creation."""
        ddl = get_ddl("p", "d", "t")
        assert "ap2_mandate_present BOOL" in ddl
        assert "ap2_mandate_keys_json JSON" in ddl
        assert "ap2_mandate_metadata_json JSON" in ddl
        assert "buyer_consent_json JSON" in ddl
        assert "ap2_mandate_raw_json JSON" in ddl

    def test_embedded_checkout_columns_present(self):
        """A3 — pin all three embedded-checkout columns. Missing one
        would silently drop the corresponding KPI."""
        ddl = get_ddl("p", "d", "t")
        assert "embedded_delegations_json JSON" in ddl
        assert "embedded_color_schemes_json JSON" in ddl
        assert "embedded_ec_color_scheme STRING" in ddl

    def test_signature_alg_columns_present(self):
        """C5c — pin both per-direction algorithm columns. Missing
        one would silently drop the corresponding column from
        auto_create_table and then break INSERTs."""
        ddl = get_ddl("p", "d", "t")
        assert "request_signature_alg STRING" in ddl
        assert "response_signature_alg STRING" in ddl

    def test_order_lifecycle_columns_present(self):
        """B8 — pin all seven order lifecycle columns: two JSON
        arrays preserve the verbatim fulfillment.events[] and
        adjustments[] for multi-event KPIs, five scalars surface the
        current state (event type + adjustment type/status + two
        TIMESTAMP columns for time-since-latest-event)."""
        ddl = get_ddl("p", "d", "t")
        assert "fulfillment_events_json JSON" in ddl
        assert "adjustments_json JSON" in ddl
        assert "latest_fulfillment_event_type STRING" in ddl
        assert "latest_fulfillment_event_at TIMESTAMP" in ddl
        assert "latest_adjustment_type STRING" in ddl
        assert "latest_adjustment_status STRING" in ddl
        assert "latest_adjustment_at TIMESTAMP" in ddl
