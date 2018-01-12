import logging
import os.path

from PyQt4.QtCore import (QSettings, QTranslator, qVersion, QCoreApplication,
                          QObject)
from PyQt4.QtGui import QAction, QIcon
# Import the code of the tools
from zThreeDiStatistics.tools.statistics import StatisticsTool
from qgis.utils import plugins


import resources  # NoQa

# Initialize Qt resources from file resources.py

log = logging.getLogger(__name__)


class ThreeDiStatistics(QObject):
    """Main Plugin Class which register toolbar and menu and tools """

    def __init__(self, iface):
        """Constructor.
        iface(QgsInterface): An interface instance which provides the hook to
        manipulate the QGIS application at run time.
        """
        log.debug('ThreeDiStatistics init')

        super(ThreeDiStatistics, self).__init__(iface)

        self.iface = iface

        # initialize plugin directory
        self.plugin_dir = os.path.join(
            os.path.dirname(__file__),
            os.path.pardir)

        # initialize locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'ThreeDiStatistics_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)

            if qVersion() > '4.3.3':
                QCoreApplication.installTranslator(self.translator)


    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API.
        We implement this ourselves since we do not inherit QObject.
        :param message: String for translation.
        :type message: str, QString
        :returns: Translated version of message.
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('ThreeDiStatistics', message)

    def add_action(
        self,
        tool_instance,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None):
        """Add a toolbar icon to the toolbar.
        :param icon_path: Path to the icon for this action. Can be a resource
            path (e.g. ':/plugins/foo/bar.png') or a normal file system path.
        :type icon_path: str
        :param text: Text that should be shown in menu items for this action.
        :type text: str
        :param callback: Function to be called when the action is triggered.
        :type callback: function
        :param enabled_flag: A flag indicating if the action should be enabled
            by default. Defaults to True.
        :type enabled_flag: bool
        :param add_to_menu: Flag indicating whether the action should also
            be added to the menu. Defaults to True.
        :type add_to_menu: bool
        :param add_to_toolbar: Flag indicating whether the action should also
            be added to the toolbar. Defaults to True.
        :type add_to_toolbar: bool
        :param status_tip: Optional text to show in a popup when mouse pointer
            hovers over the action.
        :type status_tip: str
        :param parent: Parent widget for the new action. Defaults None.
        :type parent: QWidget
        :param whats_this: Optional text to show in the status bar when the
            mouse pointer hovers over the action.
        :returns: The action that was created. Note that the action is also
            added to self.actions list.
        :rtype: QAction
        """

        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.toolbar.addAction(action)

        if add_to_menu:
            self.iface.addPluginToMenu(
                self.menu,
                action)

        setattr(tool_instance, 'action_icon', action)
        self.actions.append(action)
        return action

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""

        # get link to active threedi plugin
        log.info('ThreeDiStatistics initGui')

        try:
            tdi_plugin = plugins['ThreeDiToolbox']

        except:
            raise ImportError("For ThreeDiStatistics tool the ThreeDiToolbox plugin must be installed, "
                              "version 0.13 or higher")

        # Declare instance attributes
        self.actions = []
        self.menu = self.tr(u'&3Di toolbox')

        # Set toolbar and init a few toolbar widgets
        self.toolbar = tdi_plugin.toolbar

        # Init tools
        self.wb_tool = StatisticsTool(self.iface, tdi_plugin.ts_datasource)

        self.tools = []
        self.tools.append(self.wb_tool)

        try:
            import remote_debugger_settings
        except:
            log.info('no remote debugger activated')
            pass

        for tool in self.tools:
            self.add_action(
                tool,
                tool.icon_path,
                text=self.tr(tool.menu_text),
                callback=tool.run,
                parent=self.iface.mainWindow())

    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""

        log.info('ThreeDiStatistics unload')


        for action in self.actions:
            self.iface.removePluginMenu(
                self.menu,
                action)
            self.iface.removeToolBarIcon(action)
            self.toolbar.removeAction(action)

        for tool in self.tools:
            tool.on_unload()
