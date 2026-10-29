import datetime as dt
from decimal import Decimal

from sql_agent.grading import results_match, same_result


def test_column_order_and_row_order_do_not_matter():
    gold = [("IN", 3), ("US", 5)]
    assert results_match(gold, [(5, "US"), (3, "IN")])
    assert not results_match(gold, [("US", 5), ("IN", 3)], ordered=True)


def test_duplicates_count():
    assert not results_match([(1,), (1,), (2,)], [(1,), (2,), (2,)])


def test_lenient_allows_extra_columns_only_when_asked():
    gold = [(176,), (366,)]
    pred = [(176, "Customer 176"), (366, "Customer 366")]
    assert not results_match(gold, pred)
    assert results_match(gold, pred, lenient=True)


def test_numbers_and_rounding():
    gold = [(Decimal("3.6308171450337210"),)]
    assert results_match(gold, [(3.630817145,)])
    assert results_match(gold, [(Decimal("3.63"),)])          # the model rounded to cents
    assert not results_match(gold, [(Decimal("3.64"),)])
    assert not results_match(gold, [(Decimal("3.6"),)])        # 1 decimal is not accepted as 2dp rounding of 3.63


def test_dates_nulls_and_empty():
    assert results_match([(dt.date(2025, 1, 1), None)], [(dt.datetime(2025, 1, 1), None)])
    assert results_match([], [])
    assert not results_match([(1,)], [])


def test_same_result_is_strict():
    assert same_result([(1, "a")], [(1, "a")])
    assert not same_result([(1, "a")], [("a", 1)])
