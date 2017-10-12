import logging

log = logging.getLogger('zThreeDiStatistics')
log.setLevel(logging.DEBUG)


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load main tool class
    :param iface: QgsInterface. A QGIS interface instance.
    """
    from zThreeDiStatistics.qgistools_plugin import ThreeDiStatistics

    return ThreeDiStatistics(iface)
