# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import functools
import random

from oslo import messaging
from oslo_config import cfg
from oslo_utils import uuidutils
from osprofiler import profiler

from senlin.common import attr
from senlin.common import context
from senlin.common import exception
from senlin.common.i18n import _
from senlin.common.i18n import _LI
from senlin.common import messaging as rpc_messaging
from senlin.db import api as db_api
from senlin.engine.actions import base as action_mod
from senlin.engine import cluster as cluster_mod
from senlin.engine import dispatcher
from senlin.engine import environment
from senlin.engine import node as node_mod
from senlin.engine import scheduler
from senlin.engine import senlin_lock
from senlin.openstack.common import log as logging
from senlin.openstack.common import service
from senlin.policies import base as policy_base
from senlin.profiles import base as profile_base

LOG = logging.getLogger(__name__)

service_opts = [
    cfg.IntOpt('periodic_interval_max',
               default=60,
               help='Seconds between periodic tasks to be called'),
    cfg.BoolOpt('periodic_enable',
                default=True,
                help='Enable periodic tasks'),
    cfg.IntOpt('periodic_fuzzy_delay',
               default=60,
               help='Range of seconds to randomly delay when starting the'
                    ' periodic task scheduler to reduce stampeding.'
                    ' (Disable by setting to 0)'),
]

CONF = cfg.CONF
CONF.register_opts(service_opts)


def request_context(func):
    @functools.wraps(func)
    def wrapped(self, ctx, *args, **kwargs):
        if ctx is not None and not isinstance(ctx, context.RequestContext):
            ctx = context.RequestContext.from_dict(ctx.to_dict())
        try:
            return func(self, ctx, *args, **kwargs)
        except exception.SenlinException:
            raise messaging.rpc.dispatcher.ExpectedException()
    return wrapped


@profiler.trace_cls("rpc")
class EngineService(service.Service):
    '''Manages the running instances from creation to destruction.

    All the methods in here are called from the RPC backend.  This is
    all done dynamically so if a call is made via RPC that does not
    have a corresponding method here, an exception will be thrown when
    it attempts to call into this class.  Arguments to these methods
    are also dynamically added and will be named as keyword arguments
    by the RPC caller.
    '''

    def __init__(self, host, topic, manager=None,
                 periodic_enable=None, periodic_fuzzy_delay=None,
                 periodic_interval_max=None):

        super(EngineService, self).__init__()
        # TODO(Qiming): call environment.initialize() when environment
        # is ready
        self.host = host
        self.topic = topic
        self.dispatcher_topic = attr.ENGINE_DISPATCHER_TOPIC

        #params for periodic running task
        if periodic_interval_max is None:
            periodic_interval_max = CONF.periodic_interval_max
        if periodic_enable is None:
            periodic_enable = CONF.periodic_enable
        if periodic_fuzzy_delay is None:
            periodic_fuzzy_delay = CONF.periodic_fuzzy_delay

        self.periodic_interval_max = periodic_interval_max
        self.periodic_enable = periodic_enable
        self.periodic_fuzzy_delay = periodic_fuzzy_delay

        # The following are initialized here, but assigned in start() which
        # happens after the fork when spawning multiple worker processes
        self.engine_id = None
        self.TG = None
        self.target = None

    def start(self):
        self.engine_id = senlin_lock.BaseLock.generate_engine_id()
        self.TG = scheduler.ThreadGroupManager()

        # TODO(Yanyan): create a dispatcher for this engine thread.
        # This dispatcher will run in a greenthread and it will not
        # stop until being notified or the engine is stopped.
        self.dispatcher = dispatcher.Dispatcher(self,
                                                self.dispatcher_topic,
                                                attr.RPC_API_VERSION,
                                                self.TG)
        LOG.debug("Starting dispatcher for engine %s" % self.engine_id)

        if self.periodic_enable:
            if self.periodic_fuzzy_delay:
                initial_delay = random.randint(0, self.periodic_fuzzy_delay)
            else:
                initial_delay = None

            self.tg.add_dynamic_timer(self.periodic_tasks,
                                      initial_delay=initial_delay,
                                      periodic_interval_max=
                                      self.periodic_interval_max)

        self.dispatcher.start()

        target = messaging.Target(version=attr.RPC_API_VERSION,
                                  server=self.host,
                                  topic=self.topic)
        self.target = target
        server = rpc_messaging.get_rpc_server(target, self)
        server.start()
        environment.initialize()
        super(EngineService, self).start()

    def stop(self):
        # Stop rpc connection at first for preventing new requests
        LOG.info(_LI("Attempting to stop engine service..."))
        try:
            self.conn.close()
        except Exception:
            pass

        # Notify dispatcher to stop all action threads it started.
        self.dispatcher.stop()

        # Terminate the engine process
        LOG.info(_LI("All threads were gone, terminating engine"))
        super(EngineService, self).stop()

    def periodic_tasks(self, raise_on_error=False):
        """Tasks to be run at a periodic interval."""
        #TODO(anyone): iterate clusters and call their periodic_tasks
        return self.periodic_interval_max

    @request_context
    def get_revision(self, context):
        return cfg.CONF.revision['senlin_engine_revision']

    @request_context
    def profile_type_list(self, context):
        return environment.global_env().get_profile_types()

    @request_context
    def profile_type_spec(self, context, type_name):
        return {}

    @request_context
    def profile_type_template(self, context, type_name):
        return {}

    @request_context
    def profile_find(self, context, identity, show_deleted=False):
        '''Find a profile with the given identity (could be name or ID).'''

        if uuidutils.is_uuid_like(identity):
            profile = db_api.profile_get(context, identity,
                                         show_deleted=show_deleted)
            if not profile:
                profile = db_api.profile_get_by_name(context, identity)
        else:
            profile = db_api.profile_get_by_name(context, identity)
            if not profile:
                profile = db_api.profile_get_by_short_id(context, identity)

        if not profile:
            raise exception.ProfileNotFound(profile=identity)

        return profile

    @request_context
    def profile_list(self, context, limit=None, marker=None, sort_keys=None,
                     sort_dir=None, filters=None, show_deleted=False):
        profiles = profile_base.Profile.load_all(context, limit=limit,
                                                 marker=marker,
                                                 sort_keys=sort_keys,
                                                 sort_dir=sort_dir,
                                                 filters=filters,
                                                 show_deleted=show_deleted)

        return [p.to_dict() for p in profiles]

    @request_context
    def profile_create(self, context, name, type, spec, perm, tags):
        LOG.info(_LI('Creating profile %s:%s'), type, name)
        # validate type
        plugin = environment.global_env().get_profile(type)

        kwargs = {
            'spec': spec,
            'permission': perm,
            'tags': tags,
        }
        profile = plugin(type, name, **kwargs)
        profile.store(context)
        return profile.to_dict()

    @request_context
    def profile_get(self, context, identity):
        db_profile = self.profile_find(context, identity)
        profile = profile_base.Profile.load(context, profile=db_profile)
        return profile.to_dict()

    @request_context
    def profile_update(self, context, profile_id, name, spec, perm, tags):
        return {}

    @request_context
    def profile_delete(self, context, identity):
        db_profile = self.profile_find(context, identity)
        LOG.info(_LI('Delete profile: %s'), identity)
        profile_base.Profile.delete(context, db_profile.id)
        return None

    @request_context
    def policy_type_list(self, context):
        return environment.global_env().get_policy_types()

    @request_context
    def policy_type_spec(self, context, type_name):
        return {}

    @request_context
    def policy_type_template(self, context, type_name):
        return {}

    @request_context
    def policy_find(self, context, identity, show_deleted=False):
        '''Find a policy with the given identity (could be name or ID).'''

        if uuidutils.is_uuid_like(identity):
            policy = db_api.policy_get(context, identity,
                                       show_deleted=show_deleted)
            if not policy:
                policy = db_api.policy_get_by_name(context, identity)
        else:
            policy = db_api.policy_get_by_name(context, identity)
            if not policy:
                policy = db_api.policy_get_by_short_id(context, identity)

        if not policy:
            raise exception.PolicyNotFound(policy=identity)

        return policy

    @request_context
    def policy_list(self, context, limit=None, marker=None, sort_keys=None,
                    sort_dir=None, filters=None, show_deleted=False):
        policies = policy_base.Policy.load_all(context, limit=limit,
                                               marker=marker,
                                               sort_keys=sort_keys,
                                               sort_dir=sort_dir,
                                               filters=filters,
                                               show_deleted=show_deleted)

        return [p.to_dict() for p in policies]

    @request_context
    def policy_create(self, context, name, type, spec, level=None,
                      cooldown=None):
        LOG.info(_LI('Creating policy %s:%s'), type, name)
        plugin = environment.global_env().get_policy(type)

        kwargs = {
            'spec': spec,
            'level': level,
            'cooldown': cooldown,
        }
        policy = plugin(type, name, **kwargs)
        policy.store(context)
        return policy.to_dict()

    @request_context
    def policy_get(self, context, identity):
        db_policy = self.policy_find(context, identity)
        policy = policy_base.Policy.load(context, policy=db_policy)
        return policy.to_dict()

    @request_context
    def policy_update(self, context, identity, name, spec, level, cooldown):
        return {}

    @request_context
    def policy_delete(self, context, identity):
        db_policy = self.policy_find(context, identity)
        LOG.info(_LI('Delete policy: %s'), identity)
        policy_base.Policy.delete(context, db_policy.id)
        return None

    @request_context
    def cluster_list(self, context, limit=None, marker=None, sort_keys=None,
                     sort_dir=None, filters=None, tenant_safe=True,
                     show_deleted=False, show_nested=False):
        clusters = cluster_mod.Cluster.load_all(context, limit, marker,
                                                sort_keys, sort_dir,
                                                filters, tenant_safe,
                                                show_deleted, show_nested)

        return [cluster.to_dict() for cluster in clusters]

    @request_context
    def cluster_find(self, context, identity, show_deleted=False):
        '''Find a cluster with the given identity (could be name or ID).'''

        if uuidutils.is_uuid_like(identity):
            cluster = db_api.cluster_get(context, identity,
                                         show_deleted=show_deleted)
            # maybe the name is in uuid format, so if get by id returns None,
            # we should get the info by name again
            if not cluster:
                cluster = db_api.cluster_get_by_name(context, identity)
        else:
            cluster = db_api.cluster_get_by_name(context, identity)
            # maybe it is a short form of UUID
            if not cluster:
                cluster = db_api.cluster_get_by_short_id(context, identity)

        if not cluster:
            raise exception.ClusterNotFound(cluster=identity)

        return cluster

    @request_context
    def cluster_get(self, context, identity):
        db_cluster = self.cluster_find(context, identity)
        cluster = cluster_mod.Cluster.load(context, cluster=db_cluster)
        return cluster.to_dict()

    @request_context
    def cluster_create(self, context, name, size, profile_id, parent=None,
                       tags=None, timeout=0):
        LOG.info(_LI('Creating cluster %s'), name)
        ctx = context.to_dict()
        kwargs = {
            'user': ctx.get('username', ''),
            'project': ctx.get('tenant_id', ''),
            'parent': parent,
            'timeout': timeout,
            'tags': tags
        }

        cluster = cluster_mod.Cluster(name, profile_id, size, **kwargs)
        cluster.store(context)

        # Build an Action for cluster creation
        action = action_mod.Action(context, 'CLUSTER_CREATE',
                                   name='cluster_create_%s' % cluster.id[:8],
                                   target=cluster.id,
                                   cause=action_mod.CAUSE_RPC)
        action.store(context)

        # Notify Dispatchers that a new action has been ready.
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        # We return a cluster dictionary with an additional key carried
        result = cluster.to_dict()
        result['action'] = action.id
        return result

    @request_context
    def cluster_update(self, context, identity, size, profile_id):
        # Get the database representation of the existing cluster
        db_cluster = self.cluster_find(context, identity)
        LOG.info(_LI('Updating cluster %s'), db_cluster.name)

        cluster = cluster_mod.Cluster.load(context, cluster=db_cluster)
        if cluster.status == cluster.ERROR:
            msg = _('Updating a cluster when it is error')
            raise exception.NotSupported(feature=msg)

        if cluster.status == cluster.DELETED:
            msg = _('Updating a cluster which has been deleted')
            raise exception.NotSupported(feature=msg)

        kwargs = {
            'profile_id': profile_id
        }

        # TODO(Qiming): Hande size changes here!
        action = action_mod.Action(context, 'CLUSTER_UPDATE',
                                   target=cluster.id,
                                   cause=action_mod.CAUSE_RPC,
                                   **kwargs)
        action.store(context)

        # dispatcher.notify(context, self.dispatcher.NEW_ACTION,
        #                  None, action_id=action.id)

        return cluster.id

    @request_context
    def cluster_add_nodes(self, context, identity, nodes):
        db_cluster = self.cluster_find(context, identity)
        found = []
        not_found = []
        bad_nodes = []
        owned_nodes = []
        for node in nodes:
            try:
                db_node = self.node_find(context, node)
                # Skip node in the same cluster already
                if db_node.status != node_mod.Node.ACTIVE:
                    bad_nodes.append(db_node.id)
                elif db_node.cluster_id is not None:
                    owned_nodes.append(node)
                else:
                    found.append(db_node.id)
            except exception.NodeNotFound:
                not_found.append(node)
                pass

        error = None
        if len(bad_nodes) > 0:
            error = _("Nodes are not ACTIVE: %s") % bad_nodes
        elif len(owned_nodes) > 0:
            error = _("Nodes %s owned by other cluster, need to delete "
                      "them from those clusters first.") % owned_nodes
        elif len(not_found) > 0:
            error = _("Nodes not found: %s") % not_found
        elif len(found) == 0:
            error = _("No nodes to add: %s") % nodes

        if error is not None:
            raise exception.SenlinBadRequest(msg=error)

        action_name = 'cluster_add_nodes_%s' % db_cluster.id[:8]
        action = action_mod.Action(context, 'CLUSTER_ADD_NODES',
                                   name=action_name,
                                   target=db_cluster.id,
                                   cause=action_mod.CAUSE_RPC,
                                   inputs={'nodes': found})
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return {'action': action.id}

    @request_context
    def cluster_del_nodes(self, context, identity, nodes):
        db_cluster = self.cluster_find(context, identity)
        found = []
        not_found = []
        bad_nodes = []
        for node in nodes:
            try:
                db_node = self.node_find(context, node)
                if db_node.cluster_id != db_cluster.id:
                    bad_nodes.append(db_node.id)
                else:
                    found.append(db_node.id)
            except exception.NodeNotFound:
                not_found.append(node)
                pass

        error = None
        if len(not_found) > 0:
            error = _("Nodes %s not found") % nodes
        elif len(bad_nodes) > 0:
            error = _("Nodes %s not member of specified cluster") % bad_nodes
        elif len(found) == 0:
            error = _("No nodes specified") % nodes

        if error is not None:
            raise exception.SenlinBadRequest(msg=error)

        action_name = 'cluster_del_nodes_%s' % db_cluster.id[:8]
        action = action_mod.Action(context, 'CLUSTER_DEL_NODES',
                                   name=action_name,
                                   target=db_cluster.id,
                                   cause=action_mod.CAUSE_RPC,
                                   inputs={'nodes': found})
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return {'action': action.id}

    @request_context
    def cluster_delete(self, context, identity):
        cluster = self.cluster_find(context, identity)
        LOG.info(_LI('Deleting cluster %s'), cluster.name)

        action = action_mod.Action(context, 'CLUSTER_DELETE',
                                   name='cluster_delete_%s' % cluster.id[:8],
                                   target=cluster.id,
                                   cause=action_mod.CAUSE_RPC)
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return action.to_dict()

    def node_find(self, context, identity, show_deleted=False):
        '''Find a cluster with the given identity (could be name or ID).'''

        if uuidutils.is_uuid_like(identity):
            node = db_api.node_get(context, identity,
                                   show_deleted=show_deleted)
            if not node:
                node = db_api.node_get_by_name(context, identity)
        else:
            node = db_api.node_get_by_name(context, identity)
            if not node:
                node = db_api.node_get_by_short_id(context, identity)

        if node is None:
            raise exception.NodeNotFound(node=identity)

        return node

    @request_context
    def node_list(self, context, cluster_id=None, show_deleted=False,
                  limit=None, marker=None, sort_keys=None, sort_dir=None,
                  filters=None, tenant_safe=True):

        nodes = node_mod.Node.load_all(context, cluster_id, show_deleted,
                                       limit, marker, sort_keys, sort_dir,
                                       filters, tenant_safe)

        return [node.to_dict() for node in nodes]

    @request_context
    def node_create(self, context, name, profile_id, cluster_id=None,
                    role=None, tags=None):
        LOG.info(_LI('Creating node %s'), name)

        # Create a node instance
        node = node_mod.Node(name, profile_id, cluster_id, role=role,
                             tags=tags)
        node.store(context)

        action = action_mod.Action(context, 'NODE_CREATE',
                                   name='node_create_%s' % node.id[:8],
                                   target=node.id,
                                   cause=action_mod.CAUSE_RPC)
        action.store(context)

        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        # We return a node dictionary with an additional key (action) carried
        result = node.to_dict()
        result['action'] = action.id
        return result

    @request_context
    def node_get(self, context, identity):
        db_node = self.node_find(context, identity)
        node = node_mod.Node.load(context, node=db_node)
        return node.to_dict()

    @request_context
    def node_update(self, context, identity, name, profile_id, role, tags):
        return {}

    @request_context
    def node_delete(self, context, identity, force=False):
        db_node = self.node_find(context, identity)
        LOG.info(_LI('Deleting node %s'), identity)

        node = node_mod.Node.load(context, node=db_node)
        action = action_mod.Action(context, 'NODE_DELETE',
                                   name='node_delete_%s' % node.id[:8],
                                   target=node.id,
                                   cause=action_mod.CAUSE_RPC)
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return action.to_dict()

    @request_context
    def node_join(self, context, identity, cluster_id):
        db_node = self.node_find(context, identity)
        db_cluster = self.cluster_find(context, cluster_id)
        LOG.info(_LI('Joining node %(node)s to cluster %(cluster)s'),
                 {'node': identity, 'cluster': cluster_id})

        action = action_mod.Action(context, 'NODE_JOIN',
                                   name='node_join_%s' % db_node.id[:8],
                                   target=db_node.id,
                                   cause=action_mod.CAUSE_RPC,
                                   inputs={'cluster_id': db_cluster.id})
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return action.to_dict()

    @request_context
    def node_leave(self, context, identity):
        db_node = self.node_find(context, identity)
        LOG.info(_LI('Node %(node)s leaving cluster'), {'node': identity})

        action = action_mod.Action(context, 'NODE_LEAVE',
                                   name='node_leave_%s' % db_node.id[:8],
                                   target=db_node.id,
                                   cause=action_mod.CAUSE_RPC)
        action.store(context)
        dispatcher.notify(context, self.dispatcher.NEW_ACTION,
                          None, action_id=action.id)

        return action.to_dict()

    @request_context
    def action_find(self, context, identity, show_deleted=False):
        '''Find a cluster with the given identity (could be name or ID).'''
        # TODO(Qiming): add show_deleted support
        if uuidutils.is_uuid_like(identity):
            action = db_api.action_get(context, identity)
            if not action:
                action = db_api.action_get_by_name(context, identity)
        else:
            action = db_api.action_get_by_name(context, identity)
            if not action:
                action = db_api.action_get_by_short_id(context, identity)

        if not action:
            raise exception.ActionNotFound(action=identity)

        return action

    @request_context
    def action_list(self, context, filters=None, limit=None, marker=None,
                    sort_keys=None, sort_dir=None, show_deleted=False):

        all_actions = action_mod.Action.load_all(context, filters,
                                                 limit, marker,
                                                 sort_keys, sort_dir,
                                                 show_deleted)

        results = []
        for action in all_actions:
            raw = action.to_dict()
            del raw['context']
            results.append(raw)

        return results

    @request_context
    def action_create(self, context, name, target, action, params):
        LOG.info(_LI('Creating action %s'), name)

        # Create a node instance
        act = action_mod.Action(context, action, target,
                                name=name, params=params)
        act.store(context)

        # TODO(Anyone): Uncomment this to notify the dispatcher
        # dispatcher.notify(context, self.dispatcher.NEW_ACTION,
        #                   None, action_id=action.id)

        return act.to_dict()

    @request_context
    def action_get(self, context, identity):
        db_action = self.action_find(context, identity)
        action = action_mod.Action.load(context, action=db_action)
        return action.to_dict()
