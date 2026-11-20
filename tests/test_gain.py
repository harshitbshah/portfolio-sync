import gain


class TestBuildGainUpdates:
    def test_writes_header_formula_per_row_and_total(self):
        rows = [(2, "AAA"), (3, "BBB")]
        data, missing = gain.build_gain_updates("Tab", "E", "F", rows, {"AAA": 100.0, "BBB": 50.5}, 4)
        assert data[0] == {"range": "'Tab'!F1", "values": [["Gain %"]]}
        assert data[1] == {"range": "'Tab'!F2", "values": [['=IFERROR(E2/100.0-1,"")']]}
        assert data[2] == {"range": "'Tab'!F3", "values": [['=IFERROR(E3/50.5-1,"")']]}
        assert data[3] == {"range": "'Tab'!F4", "values": [['=IFERROR(E4/150.5-1,"")']]}
        assert missing == []

    def test_unknown_cost_leaves_row_and_total_blank(self):
        rows = [(2, "AAA"), (3, "BBB")]
        data, missing = gain.build_gain_updates("Tab", "D", "E", rows, {"AAA": 100.0}, 4)
        assert data[2]["values"] == [[""]]
        assert data[3]["values"] == [[""]]  # partial total would mislead
        assert missing == ["BBB"]

    def test_zero_cost_counts_as_unknown(self):
        _, missing = gain.build_gain_updates("Tab", "D", "E", [(2, "AAA")], {"AAA": 0.0}, None)
        assert missing == ["AAA"]

    def test_no_total_row(self):
        data, _ = gain.build_gain_updates("Tab", "E", "F", [(2, "AAA")], {"AAA": 10.0}, None)
        assert len(data) == 2

    def test_cost_rounded_to_cents(self):
        assert gain.gain_formula("E2", 29953.04619) == '=IFERROR(E2/29953.05-1,"")'


class TestTotalRowAfter:
    def test_row_below_last_position(self):
        assert gain.total_row_after([(2, "A"), (5, "B")], 6) == 6

    def test_none_when_no_row_below(self):
        assert gain.total_row_after([(2, "A")], 2) is None

    def test_none_when_no_positions(self):
        assert gain.total_row_after([], 1) is None


def test_percent_format_request_targets_one_column():
    req = gain.percent_format_request(7, 5, 18)["repeatCell"]
    assert req["range"] == {"sheetId": 7, "startRowIndex": 1, "endRowIndex": 18,
                            "startColumnIndex": 5, "endColumnIndex": 6}
    assert req["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "PERCENT"
