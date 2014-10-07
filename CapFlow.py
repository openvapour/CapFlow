
# Python
import collections

# Ryu - OpenFlow
from ryu.base import app_manager
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import HANDSHAKE_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.controller import ofp_event
from ryu.lib.packet import ethernet, ipv4, tcp, udp
from ryu.lib.packet import packet
from ryu.ofproto import ether
from ryu.ofproto import ofproto_v1_3

# Ryu - REST API
from ryu.app.wsgi import WSGIApplication
from ryu.controller import dpset

# Us
import config
import util
from rest import UserController


class Proto(object):
    ETHER_IP = 0x800
    ETHER_ARP = 0x806
    IP_UDP = 17
    IP_TCP = 6
    TCP_HTTP = 80
    UDP_DNS = 53
    TCP_DNS = 53


class CapFlow(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {
        'dpset': dpset.DPSet,
        'wsgi': WSGIApplication
    }

    def __init__(self, *args, **kwargs):
        super(CapFlow, self).__init__(*args, **kwargs)
        self.mac_to_port = collections.defaultdict(dict)
        self.authenticate = collections.defaultdict(dict)
        wsgi = kwargs['wsgi']

        wsgi.registory['UserController'] = self.authenticate
        UserController.register(wsgi)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        print "Clear rule table"
        util.delete_flow(datapath, parser.OFPMatch())

        # Send everything to ctrl
        print "Install sending to controller rule"
        util.add_flow(datapath,
            parser.OFPMatch(),
            [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
            priority=2,
        )

        # So we don't need to learn auth server location
        # TODO: this assumes we are controlling only a single switch!
        port = config.AUTH_SERVER_PORT
        self.mac_to_port[datapath.id][config.AUTH_SERVER_MAC] = port

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        nw_dst = eth.dst
        nw_src = eth.src

        dpid = datapath.id

        self.logger.info("packet at switch %s from %s to %s (port %s)",
                         dpid, nw_src, nw_dst, in_port)

        if nw_src not in self.mac_to_port[dpid]:
            print "New client: dpid", dpid, "mac", nw_src, "port", in_port
            self.mac_to_port[dpid][nw_src] = in_port
            # Be sure to not forward ARP traffic so we can learn
            # sources
            util.add_flow(datapath,
                parser.OFPMatch(
                    eth_dst=nw_src,
                    eth_type=Proto.ETHER_ARP,
                ),
                [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)],
                priority=1000,
                msg=msg, in_port=in_port,
            )

        # pass ARP through, defaults to flooding if destination unknown
        if eth.ethertype == Proto.ETHER_ARP:
            self.logger.info("ARP")
            port = self.mac_to_port[dpid].get(nw_dst, ofproto.OFPP_FLOOD)

            out = parser.OFPPacketOut(
                    datapath=datapath,
                    buffer_id=msg.buffer_id,
                    in_port=in_port,
                    actions=[parser.OFPActionOutput(port)],
                    data=msg.data,
                )
            datapath.send_msg(out)
            return

        # Non-ARP traffic to unknown L2 destination is dropped
        if nw_dst not in self.mac_to_port[dpid]:
            self.logger.info("Unknown destination!")
            return

        # We know L2 destination
        out_port = self.mac_to_port[dpid][nw_dst]

        # Helper functions (note: access variables from outer scope)
        def install_l2_src_dst(nw_src, nw_dst, out_port):
            util.add_flow(datapath,
                parser.OFPMatch(
                    eth_src=nw_src,
                    eth_dst=nw_dst,
                ),
                [parser.OFPActionOutput(out_port), ],
                priority=100,
                msg=msg, in_port=in_port,
            )

        def install_hijack(nw_src, nw_dst, ip_src, ip_dst, tp_src, tp_dst, tp):
            # TODO: we do not change port right now so it might collide with
            # other connections from the host. This is unlikely though

            if tp == "tcp":
                rev_match = parser.OFPMatch(
                              in_port=config.AUTH_SERVER_PORT,
                              eth_src=config.AUTH_SERVER_MAC,
                              eth_dst=nw_src,
                              eth_type=Proto.ETHER_IP,
                              ipv4_src=config.AUTH_SERVER_IP,
                              ipv4_dst=ip_src,
                              ip_proto=Proto.IP_TCP,
                              tcp_dst=tp_src,
                              tcp_src=tp_dst
                          )
                fwd_match = parser.OFPMatch(
                              in_port=in_port,
                              eth_src=nw_src,
                              eth_dst=nw_dst,
                              eth_type=Proto.ETHER_IP,
                              ipv4_src=ip_src,
                              ipv4_dst=ip_dst,
                              ip_proto=Proto.IP_TCP,
                              tcp_dst=tp_dst,
                              tcp_src=tp_src
                          )
            elif tp == "udp":
                rev_match = parser.OFPMatch(
                              in_port=config.AUTH_SERVER_PORT,
                              eth_src=config.AUTH_SERVER_MAC,
                              eth_dst=nw_src,
                              eth_type=Proto.ETHER_IP,
                              ipv4_src=config.AUTH_SERVER_IP,
                              ipv4_dst=ip_src,
                              ip_proto=Proto.IP_UDP,
                              udp_dst=tp_src,
                              udp_src=tp_dst
                          )
                fwd_match = parser.OFPMatch(
                              in_port=in_port,
                              eth_src=nw_src,
                              eth_dst=nw_dst,
                              eth_type=Proto.ETHER_IP,
                              ipv4_src=ip_src,
                              ipv4_dst=ip_dst,
                              ip_proto=Proto.IP_UDP,
                              udp_dst=tp_dst,
                              udp_src=tp_src
                          )

            # Reverse rule goes first
            util.add_flow(datapath, rev_match,
                [parser.OFPActionSetField(ipv4_src=ip_dst),
                 parser.OFPActionSetField(eth_src=nw_dst),
                 parser.OFPActionOutput(in_port)
                ],
                priority=1000,
            )
            # Forward rule
            util.add_flow(datapath, fwd_match,
                [parser.OFPActionSetField(ipv4_dst=config.AUTH_SERVER_IP),
                 parser.OFPActionSetField(eth_dst=config.AUTH_SERVER_MAC),
                 parser.OFPActionOutput(config.AUTH_SERVER_PORT)
                ],
                priority=1000,
                msg=msg, in_port=in_port,
            )

        def drop_unknown_ip(nw_src, nw_dst, ip_proto):
            util.add_flow(datapath,
                parser.OFPMatch(
                    eth_src=nw_src,
                    eth_dst=nw_dst,
                    eth_type=Proto.ETHER_IP,
                    ip_proto=ip_proto,
                ),
                [],
                priority=10,
                msg=msg, in_port=in_port,
            )

        if eth.ethertype != Proto.ETHER_IP:
            self.logger.info("not handling non-ip traffic")
            return

        ip = pkt.get_protocols(ipv4.ipv4)[0]

        # Is this communication allowed?
        # Allow if both src/dst are authenticated and
        l2_traffic_is_allowed = False
        
        for entry in config.WHITELIST:
            if nw_src == entry[0] and nw_dst == entry[1]:
                l2_traffic_is_allowed = True
        if self.authenticate[ip.src] and self.authenticate[ip.dst]:
            l2_traffic_is_allowed = True
        if self.authenticate[ip.src] and nw_dst == config.GATEWAY_MAC:
            l2_traffic_is_allowed = True
        if nw_src == config.GATEWAY_MAC and self.authenticate[ip.dst]:
            l2_traffic_is_allowed = True

        if l2_traffic_is_allowed:
            self.logger.info("authenticated")
            self.logger.info("Installing %s to %s bypass", nw_src, nw_dst)
            install_l2_src_dst(nw_src, nw_dst, out_port)
            return

        # Client authenticated but destination not, just block it
        if self.authenticate[ip.src]:
            self.logger.info("Auth client sending to non-auth destination blocked!")
            return
        # Client is not authenticated
        if ip.proto == 1:
            # Ignore ICMP traffic
            return
        if ip.proto == Proto.IP_UDP:
            _udp = pkt.get_protocols(udp.udp)[0]
            if _udp.dst_port == Proto.UDP_DNS:
                self.logger.info("Install DNS hijack flow")
                install_hijack(nw_src, nw_dst, ip.src, ip.dst,
                                 _udp.src_port, _udp.dst_port, "udp")
            else:
                # Ignore unknown UDP traffic
                return
        elif ip.proto == Proto.IP_TCP:
            _tcp = pkt.get_protocols(tcp.tcp)[0]
            if _tcp.dst_port == Proto.TCP_HTTP:
                self.logger.info("Install HTTP hijack flow for port %d", in_port)
                install_hijack(nw_src, nw_dst, ip.src, ip.dst,
                                 _tcp.src_port, _tcp.dst_port, "tcp")
            elif _tcp.dst_port == Proto.TCP_DNS:
                self.logger.info("Install DNS hijack flow")
                install_hijack(nw_src, nw_dst, ip.src, ip.dst,
                                 _tcp.src_port, _tcp.dst_port, "tcp")
        else:
            self.logger.info("Unknown IP proto, dropping")
            drop_unknown_ip(nw_src, nw_dst, ip.proto)
