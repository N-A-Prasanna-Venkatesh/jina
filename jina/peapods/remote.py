from typing import Callable, Dict

import grpc

from .pea import BasePea
from .zmq import Zmqlet, send_ctrl_message
from .. import __default_host__
from ..clients.python import GrpcClient
from ..helper import kwargs2list
from ..logging import get_logger
from ..proto import jina_pb2

if False:
    import argparse


class PeaSpawnHelper(GrpcClient):
    body_tag = 'pea'

    def __init__(self, args: 'argparse.Namespace'):
        super().__init__(args)
        self.ctrl_addr, self.ctrl_with_ipc = Zmqlet.get_ctrl_address(args)
        self.args = args
        self.timeout_shutdown = 10
        self.callback_on_first = True
        self.args.log_remote = False
        self._remote_logger = get_logger('🌏', **vars(self.args), fmt_str='🌏 %(message)s')

    def call(self, set_ready: Callable = None):
        """

        :param set_ready: :func:`set_ready` signal from :meth:`jina.peapods.peas.BasePea.set_ready`
        :return:
        """
        req = jina_pb2.SpawnRequest()
        self.args.log_remote = True
        getattr(req, self.body_tag).args.extend(kwargs2list(vars(self.args)))
        self.remote_logging(req, set_ready)

    def remote_logging(self, req, set_ready):
        try:
            for resp in self._stub.Spawn(req):
                if set_ready and self.callback_on_first:
                    set_ready(resp)
                    self.callback_on_first = False
                self._remote_logger.info(resp.log_record)
        except grpc.RpcError:
            pass

    def close(self):
        if not self.is_closed:
            if self.ctrl_addr:
                send_ctrl_message(self.ctrl_addr, jina_pb2.Request.ControlRequest.TERMINATE,
                                  timeout=self.timeout_shutdown)
            super().close()


class PodSpawnHelper(PeaSpawnHelper):
    body_tag = 'pod'

    def __init__(self, args: 'argparse.Namespace'):
        super().__init__(args)
        self.all_ctrl_addr = []  #: all peas control address and ports of this pod, need to be set in set_ready()

    def close(self):
        if not self.is_closed:
            for ctrl_addr in self.all_ctrl_addr:
                send_ctrl_message(ctrl_addr, jina_pb2.Request.ControlRequest.TERMINATE,
                                  timeout=self.timeout_shutdown)
            GrpcClient.close(self)


class ParsedPodSpawnHelper(PodSpawnHelper):

    def __init__(self, peas_args: Dict):
        inited = False
        for k in peas_args.values():
            if k:
                if not isinstance(k, list):
                    k = [k]
                if not inited:
                    # any pea will do, we just need its host and port_grpc
                    super().__init__(k[0])
                    inited = True
                for kk in k:
                    kk.log_remote = True
                    self.all_ctrl_addr.append(Zmqlet.get_ctrl_address(kk)[0])
        self.args = peas_args

    def call(self, set_ready: Callable = None):
        self.remote_logging(peas_args2parsed_pod_req(self.args), set_ready)


def peas_args2parsed_pod_req(peas_args: Dict):
    def pod2pea_args_list(args):
        return kwargs2list(vars(args))

    req = jina_pb2.SpawnRequest()
    if peas_args['head']:
        req.parsed_pod.head.args.extend(pod2pea_args_list(peas_args['head']))
    if peas_args['tail']:
        req.parsed_pod.tail.args.extend(pod2pea_args_list(peas_args['tail']))
    if peas_args['peas']:
        for q in peas_args['peas']:
            _a = req.parsed_pod.peas.add()
            _a.args.extend(pod2pea_args_list(q))
    return req


def parsed_pod_req2peas_args(req):
    from ..main.parser import set_pea_parser
    return {
        'head': set_pea_parser().parse_known_args(req.head.args)[0] if req.head.args else None,
        'tail': set_pea_parser().parse_known_args(req.tail.args)[0] if req.tail.args else None,
        'peas': [set_pea_parser().parse_known_args(q.args)[0] for q in req.peas] if req.peas else []
    }


class RemotePea(BasePea):
    """A RemotePea that spawns a remote :class:`BasePea`

    Useful in Jina CLI
    """

    def __init__(self, args: 'argparse.Namespace'):
        if hasattr(args, 'host') and args.host != __default_host__:
            super().__init__(args)
        else:
            raise ValueError(
                '%r requires "args.host" to be set, and it should not be %s' % (self.__class__, __default_host__))

    def post_init(self):
        pass

    def loop_body(self):
        self._remote = PeaSpawnHelper(self.args)
        self._remote.start(self.set_ready)  # auto-close after

    def close(self):
        self._remote.close()


class RemotePod(RemotePea):
    """A RemotePod that spawns a remote :class:`BasePod`

    Useful in Jina CLI
    """

    def __init__(self, args: 'argparse.Namespace'):
        if hasattr(args, 'host') and args.host != __default_host__:
            super().__init__(args)
        else:
            raise ValueError(
                '%r requires "args.host" to be set, and it should not be %s' % (self.__class__, __default_host__))

    def set_ready(self, resp):
        _rep = getattr(resp, resp.WhichOneof('body'))
        peas_args = parsed_pod_req2peas_args(_rep)
        for s in self.all_args(peas_args):
            s.host = self.args.host
            self._remote.all_ctrl_addr.append(Zmqlet.get_ctrl_address(s)[0])
        super().set_ready()

    @staticmethod
    def all_args(peas_args):
        """Get all arguments of all Peas in this BasePod. """
        return peas_args['peas'] + (
            [peas_args['head']] if peas_args['head'] else []) + (
                   [peas_args['tail']] if peas_args['tail'] else [])

    def loop_body(self):
        self._remote = PodSpawnHelper(self.args)
        self._remote.start(self.set_ready)  # auto-close after

    def close(self):
        self._remote.close()

class RemoteParsedPod(BasePea):
    """A RemoteParsedPod that spawns a remote :class:`ParsedPod`.

    Useful in Flow API
    """

    def post_init(self):
        pass

    def loop_body(self):
        self._remote = ParsedPodSpawnHelper(self.args)
        self._remote.start(self.set_ready)  # auto-close after

    def close(self):
        self._remote.close()