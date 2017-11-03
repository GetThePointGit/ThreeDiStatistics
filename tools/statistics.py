import logging
import os.path

import numpy as np
from pyspatialite import dbapi2
from qgis.core import (
    QgsMapLayerRegistry, QgsProject, QgsDataSourceURI, QgsVectorLayer)
from sqlalchemy import create_engine, MetaData
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from zThreeDiStatistics.sql_models.statistics import FlowlineStats, Node, ManholeStats, Flowline, \
    PipeStats, WeirStats, PumplineStats, StatSource
from zThreeDiStatistics.utils.statistics_database import (
    StaticsticsDatabase)

from ThreeDiToolbox.utils.user_messages import pop_up_question

log = logging.getLogger(__name__)


class StatisticsTool:
    """QGIS Plugin Implementation."""

    def __init__(self, iface, ts_datasource):
        """Constructor.
        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        # Save reference to the QGIS interface
        self.iface = iface
        self.ts_datasource = ts_datasource

        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)

        self.icon_path = ':/plugins/zThreeDiStatistics/media/icon_statistical_analysis.png'
        self.menu_text = u'Statistical Tool'

        self.plugin_is_active = False
        self.widget = None

        self.toolbox = None
        self.modeldb_engine = None
        self.modeldb_meta = None
        self.db = None

    def on_unload(self):
        """Cleanup necessary items here when plugin dockwidget is closed"""

        if self.widget is not None:
            self.widget.close()

    def get_modeldb_session(self):

        if self.modeldb_engine is None:
            self.modeldb_engine = create_engine(
                'sqlite:///{0}'.format(
                    self.ts_datasource.model_spatialite_filepath),
                module=dbapi2,
                echo=True)

            self.modeldb_meta = MetaData()
            self.modeldb_meta.reflect(bind=self.modeldb_engine)

        return sessionmaker(bind=self.modeldb_engine)()

    def get_modeldb_table(self, name):
        return self.modeldb_meta.tables[name]

    def on_close_child_widget(self):
        """Cleanup necessary items here when plugin widget is closed"""
        self.widget.closingWidget.disconnect(self.on_close_child_widget)
        self.widget = None
        self.plugin_is_active = False

    def run(self, *args, **kwargs):
        """Start processing on first selected model result (netcdf).
            Assumption is that sqlite1 already exist and is filled with flowlines, pumps and nodes.
        """
        # get links to active model database and active results (list in row)
        self.ds = self.ts_datasource.rows[0].datasource()
        self.result_db_qmodel = self.ts_datasource.rows[-1]

        # setup statistics database sqlalchemy instance and create models (if not exist) in the
        # result cache spatialite
        db_type = 'spatialite'
        db_set = {
            'db_path': self.result_db_qmodel.spatialite_cache_filepath().replace('\\', '/')
        }

        # call one of the sqlalchemy models first to detect mis configurations
        # fls = FlowlineStats()

        self.db = StaticsticsDatabase(db_set, db_type)

        if (not self.has_mod_views() and
                pop_up_question('Do you want to add views to the model database?',
                                'Add views?', )):
            self.add_modeldb_views()

        calculate_stats = True
        if (self.has_res_views() and
                not pop_up_question('Recalculate the statistics?',
                                    'Recalculate?', )):
            calculate_stats = False

        if calculate_stats:
            log.info('Create statistic models if needed.')

            self.db.create_and_check_fields()

            # # calculate the statistics
            self.get_manhole_attributes_and_statistics()
            self.create_node_views()

            self.calc_flowline_statistics()
            self.calc_pipe_and_weir_statistics()
            self.create_line_views()

            self.get_pump_attributes_and_statistics()
            self.create_pump_views()

        # add layers to QGIS map
        self.add_statistic_layers_to_map()

        log.info('Run statistic tool')

    def has_mod_views(self):

        mod_session = self.get_modeldb_session()
        view_table = self.get_modeldb_table('geometry_columns')
        return mod_session.query(view_table).filter(
            view_table.c.f_table_name == 'v2_1d_boundary_conditions_view').count() != 0

    def has_res_views(self):

        res_session = self.db.get_session()
        view_table = self.modeldb_meta.tables['views_geometry_columns']
        return res_session.query(view_table).filter(view_table.c.view_name == 'pump_stats_view').count() != 0

    def add_modeldb_views(self):

        mod_session = self.get_modeldb_session()

        mod_session.execute(
            """
                CREATE VIEW v2_pumpstation_point_view AS
                SELECT a.ROWID AS ROWID, a.id AS pump_id, a.display_name, a.code, a.classification, a.sewerage, 
                a.start_level, a.lower_stop_level, 
                       a.upper_stop_level, a.capacity, a.zoom_category, a.connection_node_start_id, 
                       a.connection_node_end_id, a.type, b.id AS connection_node_id, b.storage_area, b.the_geom
                FROM v2_pumpstation a JOIN v2_connection_nodes b ON a.connection_node_start_id = b.id;
            """)

        mod_session.execute(
            """
                INSERT INTO geometry_columns (f_table_name, f_geometry_column, type, coord_dimension, 
                  SRID, spatial_index_enabled) 
                VALUES ('v2_pumpstation_point_view', 'the_geom', 'POINT', 'XY', 4326, 0);           
            """)

        mod_session.execute(
            """
                CREATE VIEW v2_1d_lateral_view AS
                SELECT a.ROWID AS ROWID, a.id AS id, a.connection_node_id AS connection_node_id, 
                  a.timeseries AS timeseries, b.the_geom 
                FROM v2_1d_lateral a 
                JOIN v2_connection_nodes b ON a.connection_node_id = b.id;
            """)

        mod_session.execute(
            """
                INSERT INTO geometry_columns (f_table_name, f_geometry_column, type, coord_dimension, 
                  SRID, spatial_index_enabled) 
                VALUES ('v2_1d_lateral_view', 'the_geom', 'POINT', 'XY', 4326, 0);
            """)

        mod_session.execute(
            """
                CREATE VIEW v2_1d_boundary_conditions_view AS
                SELECT a.ROWID AS ROWID, a.id AS id, a.connection_node_id AS connection_node_id, 
                  a.boundary_type AS boundary_type, a.timeseries AS timeseries, b.the_geom 
                FROM v2_1d_boundary_conditions a 
                JOIN v2_connection_nodes b ON a.connection_node_id = b.id;           
            """)

        mod_session.execute(
            """
                INSERT INTO geometry_columns (f_table_name, f_geometry_column, type, coord_dimension, 
                  SRID, spatial_index_enabled) 
                VALUES ('v2_1d_boundary_conditions_view', 'the_geom', 'POINT', 'XY', 4326, 0);
            """)

        mod_session.commit()

    def get_manhole_attributes_and_statistics(self):
        """read manhole information from model spatialite and put in manhole statistic table"""

        res_session = self.db.get_session()
        mod_session = self.get_modeldb_session()

        log.info("Create mapping between result id and connection_node_id")

        nodes = res_session.query(Node.spatialite_id, Node.id).filter(Node.spatialite_id != None)
        node_mapping = {node.spatialite_id: node.id for node in nodes}

        log.info("Get information from modeldatabase about manholes")
        # get info for querying model database

        manhole_table = self.get_modeldb_table('v2_manhole')
        pipe_table = self.get_modeldb_table('v2_pipe')

        # get idx and surface level
        manhole_idx = []
        manhole_surface_level = []
        for manhole in mod_session.query(manhole_table).order_by(manhole_table.c.connection_node_id):

            if manhole.connection_node_id in node_mapping:
                manhole_idx.append(node_mapping[manhole.connection_node_id])
                manhole_surface_level.append(manhole.surface_level)
            else:
                log.warning("Manhole with id '%s' not in the results.", manhole.id)

        # create numpy arrays for index for index based reading of the netcdf and
        # surface level for calculating time on surface
        nr_manholes = len(manhole_idx)
        manhole_idx = np.array(manhole_idx)
        manhole_surface_level = np.array(manhole_surface_level)

        log.info("Read results and calculate statistics. ")
        # check if statistic is available, otherwise make empty arays for getting result from normal results
        if 's1_max' in self.ds.get_available_variables():
            agg_h_max = True
            h_max = np.empty(nr_manholes)
            h_max = -9999.0
            for i, timestamp in enumerate(self.ds.get_agg_var_timestamps('s1_max')):
                h = self.ds.get_values_by_timestep_nr(
                    's1_max',
                    i,
                    index=manhole_idx)
                # unmask result (dry cells no have -9999 values
                h_array = np.asarray(h)
                h_max = np.maximum(h_max, h_array)

        else:
            agg_h_max = False
            h_max = np.full(nr_manholes, -9999.0)

        # make empty arrays for the results
        t_water_surface = np.zeros(nr_manholes, dtype=np.float32)

        # loop over timestamps and calculate statistics
        prev_timestamp = 0.0
        for i, timestamp in enumerate(self.ds.timestamps):
            log.debug('timestamp %i - %i s', i, timestamp)

            timestep = timestamp - prev_timestamp
            prev_timestamp = timestamp
            # read data from netcdf using index to get only manholes
            h = self.ds.get_values_by_timestep_nr(
                's1',
                i - 1,
                index=manhole_idx)

            # unmask result (dry cells no have -9999 values
            h_array = np.asarray(h)
            # calculate statistics
            if not agg_h_max:
                h_max = np.maximum(h_max, h_array)

            t_water_surface[h >= manhole_surface_level] += timestep

        h_end = self.ds.get_values_by_timestep_nr(
            's1',
            len(self.ds.timestamps) - 1,
            index=manhole_idx)

        manhole_stats = []

        log.info("Make manhole statistic instances ")
        for i, manhole in enumerate(
                mod_session.query(manhole_table, func.min(pipe_table.c.sewerage_type).label('sewerage_type'))
                        .filter((manhole_table.c.connection_node_id == pipe_table.c.connection_node_start_id) |
                                        (manhole_table.c.connection_node_id == pipe_table.c.connection_node_end_id))
                        .group_by(manhole_table.c.connection_node_id)
                        .order_by(manhole_table.c.connection_node_id)):

            if manhole.connection_node_id in node_mapping:
                idx = node_mapping[manhole.connection_node_id]
                ri = int(np.where(manhole_idx == idx)[0][0])

                mhs = ManholeStats(
                    id=idx,
                    code=manhole.code,
                    display_name=manhole.display_name,
                    sewerage_type=manhole.sewerage_type,
                    bottom_level=round(manhole.bottom_level, 3),
                    surface_level=round(manhole.surface_level, 3),

                    duration_water_on_surface=round(t_water_surface[ri] / 3600, 3),
                    max_waterlevel=round(h_max[ri], 3),
                    end_waterlevel=round(h_end[ri], 3),

                    max_waterdepth_surface=round(h_max[ri] - manhole.surface_level, 3),
                    max_filling=round(100 * (h_max[ri] - manhole.bottom_level) /
                                      (manhole.surface_level - manhole.bottom_level), 1),
                    end_filling=round(100 * (h_end[ri] - manhole.bottom_level) /
                                      (manhole.surface_level - manhole.bottom_level), 1)
                )
                manhole_stats.append(mhs)

        log.info('delete old mahole statistics from database')
        res_session.execute("Delete from {0}".format(ManholeStats.__tablename__))

        log.info("Save manhole statistic instances to database ")
        res_session.bulk_save_objects(manhole_stats)
        res_session.commit()

        # store sources in database
        avg_timestep = int(self.ds.timestamps[-1] / (len(self.ds.timestamps) - 1))
        h_param = 's1'
        self.set_stat_source('manhole_stats', 'duration_water_on_surface', False, h_param, avg_timestep)
        self.set_stat_source('manhole_stats', 'end_waterlevel', False, h_param, avg_timestep)
        self.set_stat_source('manhole_stats', 'end_filling', False, h_param, avg_timestep)
        if agg_h_max:
            h_param = 's1_max'
            self.set_stat_source('manhole_stats', 'max_waterlevel', True, h_param)
            self.set_stat_source('manhole_stats', 'max_waterdepth_surface', True, h_param)
            self.set_stat_source('manhole_stats', 'max_filling', True, h_param)
        else:
            h_param = 's1'
            self.set_stat_source('manhole_stats', 'max_waterlevel', False, h_param, avg_timestep)
            self.set_stat_source('manhole_stats', 'max_waterdepth_surface', False, h_param, avg_timestep)
            self.set_stat_source('manhole_stats', 'max_filling', False, h_param, avg_timestep)

        return

    def set_stat_source(self, table, field, from_aggregated, input_param, timestep=None):
        res_session = self.db.get_session()
        t = StatSource
        instance = res_session.query(t).filter((t.table == table) & (t.field == field)).first()
        if instance:
            instance.from_agg = from_aggregated
            instance.input_param = input_param
            instance.timestep = timestep
        else:
            instance = StatSource(
                table=table,
                field=field,
                from_agg=from_aggregated,
                input_param=input_param,
                timestep=timestep
            )
            res_session.add(instance)

        res_session.commit()

    def get_agg_cum_if_available(self, parameter_name):

        if parameter_name in self.ds.get_available_variables():
            agg_cum = True
            result = self.ds.get_values_by_timestep_nr(
                parameter_name,
                len(self.ds.get_agg_var_timestamps(parameter_name)) - 1)
        else:
            agg_cum = False
            result = np.zeros(self.ds.nFlowLine)
        return result, agg_cum

    def calc_flowline_statistics(self):

        ds = self.ds
        res_session = self.db.get_session()

        log.info('create mapping to start and end nodes of flowline.')
        start_idx = []
        end_idx = []
        for flowline in res_session.query(Flowline).order_by(Flowline.id):
            start_idx.append(flowline.start_node_idx)
            end_idx.append(flowline.end_node_idx)

        start_idx = np.array(start_idx)
        end_idx = np.array(end_idx)
        log.info('read flowline results and calculate stats')

        qcum, agg_q_cum = self.get_agg_cum_if_available('q_cum')
        qcum_pos, agg_q_cum_pos = self.get_agg_cum_if_available('q_cum_positive')
        qcum_neg, agg_q_cum_neg = self.get_agg_cum_if_available('q_cum_negative')

        qmax = np.zeros(ds.nFlowLine)
        vmax = np.zeros(ds.nFlowLine)
        dh_max = np.zeros(ds.nFlowLine)
        hmax_start = np.full(ds.nFlowLine, -9999.0)
        hmax_end = np.full(ds.nFlowLine, -9999.0)

        prev_timestamp = 0.0
        for i, timestamp in enumerate(ds.timestamps):
            log.debug('timestamp %i - %i s', i, timestamp)
            timestep = timestamp - prev_timestamp
            prev_timestamp = timestamp

            q = ds.get_values_by_timestep_nr('q', i - 1)

            if not agg_q_cum:
                # todo: most accurate way to calculate cum based on normal netcdf
                qcum += q * timestep
            if not agg_q_cum_pos:
                qcum_pos += q.clip(min=0) * timestep

            if not agg_q_cum_neg:
                qcum_neg -= q.clip(max=0) * timestep

            qmax = np.maximum(qmax, np.abs(q))

            v = ds.get_values_by_timestep_nr('u1', i - 1)
            vmax = np.maximum(vmax, np.abs(v))

            h = ds.get_values_by_timestep_nr('s1', i - 1)
            h_array = h

            h_start = np.take(h_array, start_idx)
            h_end = np.take(h_array, end_idx)
            np.copyto(dh_max, np.maximum(dh_max, np.asarray(np.absolute(h_start - h_end))),
                      where=np.logical_not(np.logical_or(h_start.mask, h_end.mask)))

            hmax_start = np.maximum(hmax_start, np.asarray(h_start))
            hmax_end = np.maximum(hmax_end, np.asarray(h_end))

        qend = ds.get_values_by_timestep_nr('q', len(ds.timestamps) - 1)
        vend = ds.get_values_by_timestep_nr('u1', len(ds.timestamps) - 1)
        h_last_ts = ds.get_values_by_timestep_nr('s1', len(ds.timestamps) - 1)
        hend_start = np.take(h_last_ts, start_idx)
        hend_end = np.take(h_last_ts, end_idx)

        # save stats to the database
        log.info('prepare flowline statistics for database')
        flowline_list = []
        for i, flowline in enumerate(
                res_session.query(Flowline.id,
                                  Flowline.the_geom.ST_Transform(28992).ST_Length().label('abs_length')
                                  ).order_by(Flowline.id)):
            fls = FlowlineStats(
                id=flowline.id,
                cum_discharge=round(qcum[i], 3),
                cum_discharge_positive=round(qcum_pos[i], 3),
                cum_discharge_negative=round(qcum_neg[i], 3),
                max_discharge=round(qmax[i], 8),
                end_discharge=round(qend[i], 8),
                max_velocity=round(vmax[i], 8),
                end_velocity=round(vend[i], 8),
                max_waterlevel_head=round(dh_max[i], 4),
                max_waterlevel_start=round(hmax_start[i], 3),
                max_waterlevel_end=round(hmax_end[i], 3),
                end_waterlevel_start=round(hend_start[i], 3),
                end_waterlevel_end=round(hend_end[i], 3),
                abs_length=round(flowline.abs_length, 3)
            )
            flowline_list.append(fls)

        log.info('delete old flowline statistics from database')
        res_session.execute("Delete from {0}".format(FlowlineStats.__tablename__))

        log.info('commit flowline statistics to database')
        res_session.bulk_save_objects(flowline_list)
        res_session.commit()

        # store sources in database
        avg_timestep = int(self.ds.timestamps[-1] / (len(self.ds.timestamps) - 1))
        param = 'q'
        self.set_stat_source('flowline_stats', 'max_discharge', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'end_discharge', False, param, avg_timestep)

        param = 'u1'
        self.set_stat_source('flowline_stats', 'max_velocity', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'end_velocity', False, param, avg_timestep)

        param = 's1'
        self.set_stat_source('flowline_stats', 'max_waterlevel_head', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'max_waterlevel_start', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'max_waterlevel_end', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'end_waterlevel_start', False, param, avg_timestep)
        self.set_stat_source('flowline_stats', 'end_waterlevel_end', False, param, avg_timestep)
        self.set_stat_source('pipe_stats', 'max_filling', False, param, avg_timestep)
        self.set_stat_source('pipe_stats', 'end_filling', False, param, avg_timestep)
        self.set_stat_source('pipe_stats', 'max_hydro_gradient', False, param, avg_timestep)
        self.set_stat_source('weir_stats', 'max_overfall_height', False, param, avg_timestep)

        if agg_q_cum:
            param = 'q_cum'
            self.set_stat_source('flowline_stats', 'cum_discharge', True, param)
            self.set_stat_source('weir_stats', 'perc_volume', True, param)
        else:
            param = 'q'
            self.set_stat_source('flowline_stats', 'cum_discharge', False, param, avg_timestep)
            self.set_stat_source('weir_stats', 'perc_volume', False, param, avg_timestep)

        if agg_q_cum_pos:
            param = 'q_cum_pos'
            self.set_stat_source('flowline_stats', 'cum_discharge_positive', True, param)
            self.set_stat_source('weir_stats', 'perc_volume_positive', True, param)
        else:
            param = 'q'
            self.set_stat_source('flowline_stats', 'cum_discharge_positive', False, param, avg_timestep)
            self.set_stat_source('weir_stats', 'perc_volume_positive', False, param, avg_timestep)

        if agg_q_cum_neg:
            param = 'q_cum_neg'
            self.set_stat_source('flowline_stats', 'cum_discharge_negative', True, param)
            self.set_stat_source('weir_stats', 'perc_volume_negative', True, param)
        else:
            param = 'q'
            self.set_stat_source('flowline_stats', 'cum_discharge_negative', False, param, avg_timestep)
            self.set_stat_source('weir_stats', 'perc_volume_negative', False, param, avg_timestep)

    def calc_pipe_and_weir_statistics(self):

        res_session = self.db.get_session()
        mod_session = self.get_modeldb_session()

        # get info for querying model database
        pipe_table = self.get_modeldb_table('v2_pipe')
        profile_table = self.get_modeldb_table('v2_cross_section_definition')
        weir_table = self.get_modeldb_table('v2_weir')
        # cnode_table = self.get_modeldb_table('v2_connection_node')

        log.info("Create mapping between idx (result) and flowline_idx")
        pipes = res_session.query(Flowline.spatialite_id, Flowline.id).filter(Flowline.type == 'v2_pipe')
        pipes_mapping = {pipe.spatialite_id: pipe.id for pipe in pipes}

        log.info('create pipe statistic instances.')
        pipe_stats = []

        for pipe in (mod_session.query(pipe_table, profile_table.c.shape, profile_table.c.height, profile_table.c.width)
                             .filter(pipe_table.c.cross_section_definition_id == profile_table.c.id)):
            if not pipe.id in pipes_mapping:
                log.warning("no result for pipe with spatialite id %i",
                            pipe.id)
            idx = pipes_mapping[pipe.id]
            if pipe.shape in (1, 2) and pipe.width is not None:
                height = max(pipe.width.split(' '))
            elif pipe.shape in (5, 6) and pipe.height is not None:
                height = max(pipe.height.split(' '))
            else:
                height = None

            ps = PipeStats(
                id=idx,
                code=pipe.code,
                display_name=pipe.display_name,
                sewerage_type=pipe.sewerage_type,
                invert_level_start=pipe.invert_level_start_point,
                invert_level_end=pipe.invert_level_end_point,
                profile_height=height
            )
            pipe_stats.append(ps)

        log.info('delete old pipe statistics from database')
        res_session.execute("Delete from {0}".format(PipeStats.__tablename__))

        log.info('commit pipe characteristics to database')
        res_session.bulk_save_objects(pipe_stats)
        res_session.commit()

        log.info("Create mapping between idx (result) and weir spatialite_id")
        res_session = self.db.get_session()
        weirs = res_session.query(Flowline.spatialite_id, Flowline.id).filter(Flowline.type == 'v2_weir')
        weirs_mapping = {weir.spatialite_id: weir.id for weir in weirs}

        log.info('create weir statistic instances.')
        weir_stats = []
        for weir in mod_session.query(weir_table):
            idx = weirs_mapping[weir.id]

            ws = WeirStats(
                id=idx,
                code=weir.code,
                display_name=weir.display_name,
                crest_level=weir.crest_level
            )
            weir_stats.append(ws)

        log.info('delete old weir statistics from database')
        res_session.execute("Delete from {0}".format(WeirStats.__tablename__))

        log.info('commit weir characteristics to database')
        res_session.bulk_save_objects(weir_stats)
        res_session.commit()

        def get_filling(start_level, end_level, start_invert_level, end_invert_level, profile_height):
            if None in [start_level, end_level, start_invert_level, end_invert_level, profile_height]:
                return None

            fill_start = (start_level - start_invert_level) / profile_height
            # make sure it is between 0 and 1
            fill_start = max(0, min(1, fill_start))
            fill_end = (end_level - end_invert_level) / profile_height
            # make sure it is between 0 and 1
            fill_end = max(0, min(1, fill_end))
            # return average
            return round(100 * (fill_start + fill_end) / 2, 3)

        for pipe in res_session.query(PipeStats).join(Flowline).join(FlowlineStats):
            if (pipe.flowline.stats.abs_length is not None and
                        pipe.flowline.stats.abs_length > 0 and pipe.flowline.stats.max_waterlevel_head is not None):
                pipe.max_hydro_gradient = round(100 * (
                    pipe.flowline.stats.max_waterlevel_head / pipe.flowline.stats.abs_length), 3)

            pipe.max_filling = get_filling(
                pipe.flowline.stats.max_waterlevel_start,
                pipe.flowline.stats.max_waterlevel_end,
                pipe.invert_level_start,
                pipe.invert_level_end,
                pipe.profile_height
            )
            pipe.end_filling = get_filling(
                pipe.flowline.stats.end_waterlevel_start,
                pipe.flowline.stats.end_waterlevel_end,
                pipe.invert_level_start,
                pipe.invert_level_end,
                pipe.profile_height
            )

        res_session.commit()

        # get max cum of weir
        max_cum_discharge = res_session \
            .query(func.max(func.abs(FlowlineStats.cum_discharge))) \
            .filter(FlowlineStats.id == WeirStats.id).scalar()
        max_cum_discharge_pos = res_session \
            .query(func.max(FlowlineStats.cum_discharge_positive)) \
            .filter(FlowlineStats.id == WeirStats.id).scalar()
        max_cum_discharge_neg = res_session \
            .query(func.max(FlowlineStats.cum_discharge_negative)) \
            .filter(FlowlineStats.id == WeirStats.id).scalar()

        for weir in res_session.query(WeirStats).join(Flowline).join(FlowlineStats):
            weir.perc_volume = round(100 * weir.flowline.stats.cum_discharge / max_cum_discharge, 2)
            weir.perc_volume_positive = round(100 * weir.flowline.stats.cum_discharge_positive / max_cum_discharge_pos,
                                              2)
            weir.perc_volume_negative = round(100 * weir.flowline.stats.cum_discharge_negative / max_cum_discharge_neg,
                                              2)
            weir.max_overfall_height = round(max(weir.flowline.stats.max_waterlevel_start,
                                                 weir.flowline.stats.max_waterlevel_end) - weir.crest_level, 3)

        res_session.commit()

    def get_pump_attributes_and_statistics(self):
        """read manhole information from model spatialite and put in manhole statistic table"""
        res_session = self.db.get_session()
        log.info("Get information from modeldatabase about pumps")
        # get info for querying model database
        mod_session = self.get_modeldb_session()
        pump_table = self.get_modeldb_table('v2_pumpstation')

        # get idx and surface level
        manhole_idx = []
        pump_capacity = []
        for pump in mod_session.query(pump_table).order_by(pump_table.c.id):
            pump_capacity.append(pump.capacity)

        # create numpy arrays for index for index based reading of the netcdf and
        # surface level for calculating time on surface
        nr_pumps = len(pump_capacity)

        log.info("Read results and calculate statistics. ")
        # make empty arrays for the results

        q_cum, agg_q_cum = self.get_agg_cum_if_available('q_pump_cum')

        q_max = np.zeros(nr_pumps, dtype=np.float32)

        # loop over timestamps and calculate statistics
        prev_timestamp = 0.0
        for i, timestamp in enumerate(self.ds.timestamps):
            log.debug('timestamp %i - %i s', i, timestamp)

            timestep = timestamp - prev_timestamp
            prev_timestamp = timestamp
            # read data from netcdf using index to get only manholes
            q = self.ds.get_values_by_timestep_nr(
                'q_pump',
                i - 1)
            # calculate statistics
            if not agg_q_cum:
                q_cum += q_cum * timestep

            q_max = np.maximum(q_max, q)

        q_end = self.ds.get_values_by_timestep_nr(
            'q_pump',
            len(self.ds.timestamps) - 1)

        pump_stats = []
        log.info("Make Pumpline statistic instances ")
        # no idmapping info in pumpline model, so get from idmapping file
        id_mapping = self.ds.id_mapping['v2_pumpstation']

        for i, pump in enumerate(mod_session.query(pump_table).order_by(pump_table.c.id)):
            ps = PumplineStats(
                id=id_mapping[str(pump.id)] - 1,
                spatialite_id=pump.id,
                code=pump.code,
                display_name=pump.display_name,
                capacity=pump.capacity / 1000,
                cum_discharge=round(q_cum[i], 3),
                end_discharge=round(q_end[i], 8),
                max_discharge=round(q_max[i], 8),
                duration_pump_on_max=round(q_cum[i] / (pump.capacity / 1000) / 3600, 3),
                perc_max_discharge=round(100 * q_max[i] / (pump.capacity / 1000), 2),
                perc_end_discharge=round(100 * q_end[i] / (pump.capacity / 1000), 2)
            )
            pump_stats.append(ps)

        log.info('delete old pumpline statistics from database')
        res_session.execute("Delete from {0}".format(PumplineStats.__tablename__))

        log.info("Save pumpline statistic instances to database ")
        res_session.bulk_save_objects(pump_stats)
        res_session.commit()

        # store sources in database
        avg_timestep = int(self.ds.timestamps[-1] / (len(self.ds.timestamps) - 1))
        param = 'q_pump'
        self.set_stat_source('pumpline_stats', 'end_discharge', False, param, avg_timestep)
        self.set_stat_source('pumpline_stats', 'max_discharge', False, param, avg_timestep)
        self.set_stat_source('pumpline_stats', 'perc_end_discharge', False, param, avg_timestep)

        if agg_q_cum:
            param = 'q_pump_cum'
            self.set_stat_source('pumpline_stats', 'cum_discharge', True, param)
            self.set_stat_source('pumpline_stats', 'duration_pump_on_max', True, param)
            self.set_stat_source('pumpline_stats', 'perc_max_discharge', True, param)
        else:
            param = 'q_pump'
            self.set_stat_source('pumpline_stats', 'cum_discharge', False, param, avg_timestep)
            self.set_stat_source('pumpline_stats', 'duration_pump_on_max', False, param, avg_timestep)
            self.set_stat_source('pumpline_stats', 'perc_max_discharge', False, param, avg_timestep)

        return

    def create_line_views(self):

        session = self.db.get_session()

        # flowline stat view
        session.execute(
            """CREATE VIEW IF NOT EXISTS flowline_stats_view 
               (id, inp_id, spatialite_id, TYPE, start_node_idx, end_node_idx, the_geom, 
                cum_discharge, cum_discharge_positive, cum_discharge_negative, 
                max_discharge, end_discharge, 
                max_velocity, end_velocity,
                max_waterlevel_head, max_waterlevel_start, max_waterlevel_end) AS 
               SELECT f.id, f.inp_id, f.spatialite_id, f.type, f.start_node_idx, f.end_node_idx, f.the_geom,
                fs.cum_discharge, fs.cum_discharge_positive, fs.cum_discharge_negative, 
                fs.max_discharge, fs.end_discharge, 
                fs.max_velocity, fs.end_velocity,
                fs.max_waterlevel_head, fs.max_waterlevel_start, fs.max_waterlevel_end
                FROM flowlines f, flowline_stats fs 
                WHERE f.id = fs.id;"""
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'flowline_stats_view';
            """
        )
        session.execute(
            """      
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('flowline_stats_view', 'the_geom', 'id', 'flowlines', 'the_geom', 1);
            
            """
        )

        session.commit()

        # pipe stat view
        session.execute(
            """CREATE VIEW IF NOT EXISTS pipe_stats_view 
               (id, inp_id, spatialite_id, TYPE, start_node_idx, end_node_idx, the_geom,
                code, display_name, sewerage_type, abs_length, invert_level_start, invert_level_end, profile_height,
                max_hydro_gradient, max_filling, end_filling,
                cum_discharge, cum_discharge_positive, cum_discharge_negative, 
                max_discharge, end_discharge, 
                max_velocity, end_velocity,
                max_waterlevel_head, max_waterlevel_start, max_waterlevel_end) AS 
               SELECT f.id, f.inp_id, f.spatialite_id, f.type, f.start_node_idx, f.end_node_idx, f.the_geom,
                ps.code, ps.display_name, ps.sewerage_type, fs.abs_length, ps.invert_level_start, ps.invert_level_end, 
                ps.profile_height,
                ps.max_hydro_gradient, ps.max_filling, ps.end_filling,   
                fs.cum_discharge, fs.cum_discharge_positive, fs.cum_discharge_negative, 
                fs.max_discharge, fs.end_discharge, 
                fs.max_velocity, fs.end_velocity,
                fs.max_waterlevel_head, fs.max_waterlevel_start, fs.max_waterlevel_end
                FROM flowlines f, flowline_stats fs, pipe_stats ps
                WHERE f.id = fs.id AND f.id = ps.id;"""
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'pipe_stats_view';
            """
        )
        session.execute(
            """ 
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('pipe_stats_view', 'the_geom', 'id', 'flowlines', 'the_geom', 1);

            """
        )
        session.commit()

        # dwa+mixed of pipestats
        session.execute(
            """
            CREATE VIEW IF NOT EXISTS pipe_stats_dwa_mixed_view 

             AS 
            SELECT *
             FROM pipe_stats_view
             WHERE pipe_stats_view.sewerage_type IN (0, 2);
            """
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'pipe_stats_dwa_mixed_view';
            """
        )
        session.execute(
            """
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('pipe_stats_dwa_mixed_view', 'the_geom', 'id', 'flowlines', 'the_geom', 1);
            """
        )
        session.commit()

        # rwa views of pipestats
        session.execute(
            """
            CREATE VIEW IF NOT EXISTS pipe_stats_rwa_view 
             AS 
            SELECT *
             FROM pipe_stats_view
             WHERE pipe_stats_view.sewerage_type IN (1);
            """
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'pipe_stats_rwa_view';          
            """
        )
        session.execute(
            """      
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('pipe_stats_rwa_view', 'the_geom', 'id', 'flowlines', 'the_geom', 1);
            """
        )
        session.commit()

        # weir stat view
        session.execute(
            """CREATE VIEW IF NOT EXISTS weir_stats_view 
               (id, inp_id, spatialite_id, TYPE, start_node_idx, end_node_idx, the_geom,
                code, display_name,
                perc_volume, perc_volume_positive, perc_volume_negative, max_overfall_height,
                cum_discharge, cum_discharge_positive, cum_discharge_negative, 
                max_discharge, end_discharge, 
                max_velocity, end_velocity,
                max_waterlevel_head, max_waterlevel_start, max_waterlevel_end) AS 
               SELECT f.id, f.inp_id, f.spatialite_id, f.type, f.start_node_idx, f.end_node_idx, f.the_geom,
                ws.code, ws.display_name,
                ws.perc_volume, ws.perc_volume_positive, ws.perc_volume_negative, ws.max_overfall_height,  
                fs.cum_discharge, fs.cum_discharge_positive, fs.cum_discharge_negative, 
                fs.max_discharge, fs.end_discharge, 
                fs.max_velocity, fs.end_velocity,
                fs.max_waterlevel_head, fs.max_waterlevel_start, fs.max_waterlevel_end
                FROM flowlines f, flowline_stats fs, weir_stats ws
                WHERE f.id = fs.id AND f.id = ws.id;"""
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'weir_stats_view';          
            """
        )
        session.execute(
            """
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('weir_stats_view', 'the_geom', 'id', 'flowlines', 'the_geom', 1);
    
            """
        )

        session.commit()

    def create_node_views(self):
        session = self.db.get_session()

        # manhole stat view
        session.execute(
            """CREATE VIEW IF NOT EXISTS manhole_stats_view 
               (id, inp_id, spatialite_id, featuretype, TYPE, the_geom, 
                code, display_name, sewerage_type, bottom_level, surface_level,
                duration_water_on_surface, max_waterlevel, end_waterlevel, max_waterdepth_surface, end_filling
               ) AS 
               SELECT n.id, n.inp_id, n.spatialite_id, n.featuretype, n.type, n.the_geom,
                mst.code, mst.display_name, mst.sewerage_type, mst.bottom_level, mst.surface_level,
                mst.duration_water_on_surface, mst.max_waterlevel, mst.end_waterlevel, mst.max_waterdepth_surface, 
                mst.end_filling
                FROM nodes n, manhole_stats mst 
                WHERE n.id = mst.id;"""
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'manhole_stats_view';
            """
        )
        session.execute(
            """
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('manhole_stats_view', 'the_geom', 'id', 'nodes', 'the_geom', 1);
            """
        )
        session.commit()

        # dwa+mixed  of manholestats
        session.execute(
            """
            CREATE VIEW IF NOT EXISTS manhole_stats_dwa_mixed_view

             AS 
            SELECT *
             FROM manhole_stats_view
             WHERE manhole_stats_view.sewerage_type IN (0, 2);
            """
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'manhole_stats_dwa_mixed_view';
            """
        )
        session.execute(
            """
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('manhole_stats_dwa_mixed_view', 'the_geom', 'id', 'nodes', 'the_geom', 1);
            """
        )
        session.commit()

        # rwa views of manholestats
        session.execute(
            """
            CREATE VIEW IF NOT EXISTS manhole_stats_rwa_view 
             AS 
            SELECT *
             FROM manhole_stats_view
             WHERE manhole_stats_view.sewerage_type IN (1);
            """
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'manhole_stats_rwa_view';          
            """
        )
        session.execute(
            """      
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('manhole_stats_rwa_view', 'the_geom', 'id', 'nodes', 'the_geom', 1);
            """
        )
        session.commit()

    def create_pump_views(self):
        session = self.db.get_session()

        # manhole stat view
        session.execute(
            """CREATE VIEW IF NOT EXISTS pump_stats_view 
               (id, node_idx1, node_idx2, the_geom, 
                spatialite_id, code, display_name, capacity,
                cum_discharge, end_discharge, max_discharge, perc_max_discharge, perc_end_discharge, 
                duration_pump_on_max
               ) AS 
               SELECT p.id, p.node_idx1, p.node_idx2, p.the_geom,
                ps.spatialite_id, ps.code, ps.display_name, ps.capacity,
                ps.cum_discharge, ps.end_discharge, ps.max_discharge, ps.perc_max_discharge, ps.perc_end_discharge,
                ps.duration_pump_on_max
                FROM pumplines p, pumpline_stats ps
                WHERE p.id = ps.id;
        """
        )
        session.execute(
            """
            DELETE FROM views_geometry_columns WHERE view_name = 'pump_stats_view';
            """
        )
        session.execute(
            """
            INSERT INTO views_geometry_columns (view_name, view_geometry, view_rowid, f_table_name, 
            f_geometry_column, read_only)
            VALUES('pump_stats_view', 'the_geom', 'id', 'pumplines', 'the_geom', 1);
            """
        )
        session.commit()

    def add_statistic_layers_to_map(self):
        # {layer_name: [(name, layer, field, style,), ...], ... }
        styled_layers = {
            'leidingen': [
                ('debiet (max)', 'pipe_stats_view', 'max_discharge', 'leiding_1'),
                ('stroomsnelheid (max)', 'pipe_stats_view', 'max_velocit', 'leiding_1'),
                ('verhang [cm/m] (max)', 'pipe_stats_view', 'max_hydro_gradient', 'leiding_1'),
                ('stroomsnelheid (end)', 'pipe_stats_view', 'end_velocity', 'leiding_2'),
                ('stroomsnelheid DWA en Gemengd (end)', 'pipe_stats_dwa_mixed_view', 'end_velocity', 'leiding_2'),
                ('stroomsnelheid RWA (end)', 'pipe_stats_rwa_view', 'end_velocity', 'leiding_2'),
            ],
            'putten': [
                ('vullingsgraad (max)', 'manhole_stats_view', 'max_filling', 'vullingsgraad_put'),
                ('vullingsgraad DWA en gemengd (end)', 'manhole_stats_dwa_mixed_view', 'end_filling',
                 'vullingsgraad_put'),
                ('vullingsgraad RWA (end)', 'manhole_stats_rwa_view', 'end_filling', 'vullingsgraad_put'),
                ('duur wos [uren]', 'manhole_stats_view', 'duration_water_on_surface', 'wos'),
                ('waterstand op straat (max)', 'manhole_stats_view', 'max_waterdepth_surface', 'put_0'),
                ('waterstand op straat DWA en gemengd(max)', 'manhole_stats_dwa_mixed_view',
                 'max_waterdepth_on_surface', 'put_0'),
                ('waterstand op straat RWA (max)', 'manhole_stats_rwa_view', 'max_waterdepth_on_surface', 'put_0'),
            ],
            'pumps': [
                ('perc gemaalcapaciteit (max)', 'pump_stats_view', 'perc_max_discharge', 'pumps_100'),
                ('perc gemaalcapaciteit (end)', 'pump_stats_view', 'perc_end_discharge', 'pumps_100'),
                ('totaal verpompt volume [m3]', 'pump_stats_view', 'cum_discharge', 'pumps_100'),
                ('pompduur op maximale capaciteit [uren]', 'pump_stats_view', 'duration_pump_on_max', 'pumps_8'),
            ],
            'overstorten': [
                ('overstortende straal (max)', 'weir_stats_view', 'max_overfall_height', 'overstort'),
            ]
        }
                ('overstortvolume perc tov max (cum)', 'weir_stats_view', 'perc_volume', 'overstort_perc'),
                ('overstortvolume positief perc tov max (cum)', 'weir_stats_view', 'perc_volume_positive', 'overstort_perc'),
                ('overstortvolume negatief perc tov max (cum)', 'weir_stats_view', 'perc_volume_negative', 'overstort_perc'),

        root = QgsProject.instance().layerTreeRoot()

        stats_group_name = 'statistics'
        stat_group = root.findGroup(stats_group_name)
        if stat_group is None:
            stat_group = root.insertGroup(0, stats_group_name)

        stat_group.removeAllChildren()

        for group, layers in styled_layers.items():
            qgroup = stat_group.insertGroup(0, group)

            for layer in layers:
                uri = QgsDataSourceURI()
                uri.setDatabase(self.result_db_qmodel.spatialite_cache_filepath().replace('\\', '/'))
                uri.setDataSource('', layer[1], 'the_geom')

                vector_layer = QgsVectorLayer(uri.uri(), layer[0], 'spatialite')

                if vector_layer.isValid():
                    style_path = os.path.join(
                        os.path.dirname(os.path.realpath(__file__)),
                        os.path.pardir,
                        'layer_styles',
                        'stats',
                        layer[3] + '.qml')
                    style = file(style_path, 'r').read()

                    # replace by column name
                    style = style.replace('<<variable>>', layer[2])

                    new_style_path = os.path.join(
                        os.path.dirname(os.path.realpath(__file__)),
                        os.path.pardir,
                        'layer_styles',
                        'stats',
                        'cr_' + layer[3] + '_' + layer[2] + '.qml')

                    new_style_file = file(new_style_path, 'w')
                    new_style_file.write(style)
                    new_style_file.close()

                    vector_layer.loadNamedStyle(new_style_path)

                    QgsMapLayerRegistry.instance().addMapLayer(
                        vector_layer,
                        False)

                    qgroup.insertLayer(0, vector_layer)
