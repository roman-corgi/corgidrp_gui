import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def catalog():
    from corgidrp_gui.core.steps import StepCatalog
    return StepCatalog()


@pytest.fixture(scope="session")
def mock_l1_files(tmp_path_factory):
    """Two small mock L1 frames made with corgidrp.mocks (headers only matter here)."""
    import corgidrp.mocks as mocks
    d = tmp_path_factory.mktemp("l1")
    ds = mocks.create_prescan_files(filedir=str(d), numfiles=2)
    return sorted(str(f.filepath) for f in ds)


@pytest.fixture(scope="session", autouse=True)
def _close_qt_windows_at_exit():
    yield
    try:
        from qtpy import QtWidgets
    except ImportError:  # pragma: no cover
        return
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    app.processEvents()
    app.processEvents()


@pytest.fixture
def nonlin_caldb(tmp_path, monkeypatch, mock_l1_files):
    """A fresh CalDB holding a single NonLinearityCalibration, built from corgidrp's packaged
    TVAC table; corgidrp is pointed at it. Returns (caldb path, calibration file path)."""
    import numpy as np
    import corgidrp
    import corgidrp.caldb as caldb
    import corgidrp.data as cdata
    import corgidrp.mocks as mocks
    path = str(tmp_path / "nonlin_caldb.csv")
    monkeypatch.setattr(corgidrp, "caldb_filepath", path)
    table = os.path.join(os.path.dirname(corgidrp.__file__), "data", "default_calibs", "nonlin_table_240322.txt")
    pri, ext, _err, _dq = mocks.create_default_calibration_product_headers()
    cal = cdata.NonLinearityCalibration(np.genfromtxt(table, delimiter=","), pri_hdr=pri, ext_hdr=ext,
                                        input_dataset=cdata.Dataset(list(mock_l1_files)))
    (tmp_path / "cals").mkdir()
    cal.save(filedir=str(tmp_path / "cals"), filename="cgi_0000000000000000000_20260101t0000000_nln_cal.fits")
    caldb.CalDB(path).create_entry(cal)
    return path, cal.filepath
