# Copyright (C) 2007 AG Projects.
#

"""XCAP authentication module"""

# XXX this module should be either renamed or refactored as it does more then just auth.

from xcap import tweaks

import urlparse
from zope.interface import Interface, implements

from twisted.internet import defer
from twisted.python import failure
from twisted.cred import credentials, portal, checkers, error as credError
from twisted.web2 import http, server, stream, responsecode
from twisted.web2.auth.wrapper import HTTPAuthResource, UnauthorizedResponse

from application.configuration.datatypes import StringList, NetworkRangeList
from application import log

from xcap import __version__
from xcap.config import *
from xcap.appusage import getApplicationForURI, namespaces
from xcap.dbutil import connectionForURI
from xcap.errors import ResourceNotFound
from xcap.uri import XCAPUser, XCAPUri, NodeParsingError, Error as URIError


# body of 404 error message to render when user requests xcap-root
# it's html, because XCAP root is often published on the web.
# NOTE: there're no plans to convert other error messages to html.
# Since a web-browser is not the primary tool for accessing XCAP server, text/plain
# is easier for clients to present to user/save to logs/etc.
WELCOME = ('<html><head><title>Not Found</title></head>'
           '<body><h1>Not Found</h1>XCAP server does not serve anything '
           'directly under XCAP Root URL. You have to be more specific.'
           '<br><br>'
           '<address><a href="http://www.openxcap.org">OpenXCAP/%s</address>'
           '</body></html>') % __version__


class AuthenticationConfig(ConfigSection):
    _datatypes = {'trusted_peers': StringList,
                  'default_realm': str}
    default_realm = None
    trusted_peers = []

configuration = ConfigFile()

def list_contains_uri(uris, uri):
    for u in uris:
        if u == uri:
            return True
        if uri.startswith(u) or u.startswith(uri):
            log.warn("XCAP Root URI rejected: %r (looks like %r)" % (uri, u))
            return True

class XCAPRootURIs(tuple):
    """Configuration data type. A tuple of defined XCAP Root URIs is extracted from
       the configuration file."""
    def __new__(typ):
        uris = []
        def add(uri):
            scheme, host, path, params, query, fragment = urlparse.urlparse(uri)
            if not scheme or not host or scheme not in ("http", "https"):
                log.warn("Invalid XCAP Root URI: %r" % uri)
            elif not list_contains_uri(uris, uri):
                uris.append(uri)
        for uri in configuration.get_values_unique('Server', 'root'):
            add(uri)
        if not uris:
            import socket
            add('http://' + socket.getfqdn())
        if not uris:
            raise ResourceNotFound("At least one XCAP Root URI must be defined")
        return tuple(uris)

class ServerConfig:
    root_uris = XCAPRootURIs()

configuration.read_settings('Authentication', AuthenticationConfig)

print 'Supported Root URIs: %s' % ', '.join(ServerConfig.root_uris)

def parseNodeURI(node_uri, default_realm):
    """Parses the given Node URI, containing the XCAP root, document selector,
       and node selector, and returns an XCAPUri instance if succesful."""
    xcap_root = None
    for uri in ServerConfig.root_uris:
        if node_uri.startswith(uri):
            xcap_root = uri
            break
    if xcap_root is None:
        raise ResourceNotFound("XCAP root not found for URI: %s" % node_uri)
    resource_selector = node_uri[len(xcap_root):]
    if not resource_selector or resource_selector=='/':
        raise ResourceNotFound(WELCOME)
    r = XCAPUri(xcap_root, resource_selector, namespaces)
    if r.user.domain is None:
        r.user.domain = default_realm
    return r


class ITrustedPeerCredentials(credentials.ICredentials):

    def checkPeer(self, trusted_peers):
        pass


class TrustedPeerCredentials:
    implements(ITrustedPeerCredentials)

    def __init__(self, peer):
        self.peer = peer

    def checkPeer(self, trusted_peers):
        return self.peer in trusted_peers

## credentials checkers

class TrustedPeerChecker:

    implements(checkers.ICredentialsChecker)
    credentialInterfaces = (ITrustedPeerCredentials,)

    def __init__(self, trusted_peers):
        self.trusted_peers = trusted_peers

    def requestAvatarId(self, credentials):
        """Return the avatar ID for the credentials which must have a 'peer' attribute,
           or an UnauthorizedLogin in case of a failure."""
        if credentials.checkPeer(self.trusted_peers):
            return defer.succeed(credentials.peer)
        return defer.fail(credError.UnauthorizedLogin())

## avatars

class IAuthUser(Interface):
    pass

class ITrustedPeer(Interface):
    pass

class AuthUser(str):
    """Authenticated XCAP User avatar."""
    implements(IAuthUser)

class TrustedPeer(str):
    """Trusted peer avatar."""
    implements(ITrustedPeer)

## realm

class XCAPAuthRealm(object):
    """XCAP authentication realm. Receives an avatar ID (a string identifying the user)
       and a list of interfaces the avatar needs to support. It returns an avatar that
       encapsulates data about that user."""
    implements(portal.IRealm)

    def requestAvatar(self, avatarId, mind, *interfaces):
        if IAuthUser in interfaces:
            return IAuthUser, AuthUser(avatarId)
        elif ITrustedPeer in interfaces:
            return ITrustedPeer, TrustedPeer(avatarId)

        raise NotImplementedError("Only IAuthUser and ITrustedPeer interfaces are supported")

def get_cred(request, default_realm):
    auth = request.headers.getHeader('authorization')
    if auth:
        typ, data = auth
        if typ == 'basic':
            return data.decode('base64').split(':', 1)[0], default_realm
        elif typ == 'digest':
            raise NotImplementedError
    return None, default_realm

## authentication wrapper for XCAP resources
class XCAPAuthResource(HTTPAuthResource):

    def allowedMethods(self):
        return ('GET', 'PUT', 'DELETE')

    def _updateRealm(self, realm):
        """Updates the realm of the attached credential factories."""
        for factory in self.credentialFactories.values():
            factory.realm = realm

    def authenticate(self, request):
        """Authenticates an XCAP request."""
        uri = request.scheme + "://" + request.host + request.uri
        xcap_uri = parseNodeURI(uri, AuthenticationConfig.default_realm)
        request.xcap_uri = xcap_uri
        if xcap_uri.doc_selector.context=='global':
            return defer.succeed(self.wrappedResource)

        ## For each request the authentication realm must be
        ## dinamically deducted from the XCAP request URI
        realm = xcap_uri.user.domain

        if not xcap_uri.user.username:
            # for 'global' requests there's no username@domain in the URI,
            # so we will use username and domain from Authorization header
            xcap_uri.user.username, xcap_uri.user.domain = get_cred(request, AuthenticationConfig.default_realm)

        self._updateRealm(realm)
        remote_addr = request.remoteAddr.host
        if AuthenticationConfig.trusted_peers:
            return self.portal.login(TrustedPeerCredentials(remote_addr),
                                     None,
                                     ITrustedPeer
                                     ).addCallbacks(self._loginSucceeded,
                                                    self._trustedPeerLoginFailed,
                                                    (request,), None,
                                                    (request,), None)
        return HTTPAuthResource.authenticate(self, request)

    def _trustedPeerLoginFailed(self, result, request):
        """If the peer is not trusted, fallback to HTTP basic/digest authentication."""
        return HTTPAuthResource.authenticate(self, request)

    def _loginSucceeded(self, avatar, request):
        """Authorizes an XCAP request after it has been authenticated."""
        
        interface, avatar_id = avatar ## the avatar is the authenticated XCAP User
        xcap_uri = request.xcap_uri

        application = getApplicationForURI(xcap_uri)

        if not application:
            raise ResourceNotFound

        if interface is IAuthUser and application.is_authorized(XCAPUser.parse(avatar_id), xcap_uri):
            return HTTPAuthResource._loginSucceeded(self, avatar, request)
        elif interface is ITrustedPeer:
            return HTTPAuthResource._loginSucceeded(self, avatar, request)
        else:
            return failure.Failure(
                      http.HTTPError(
                        UnauthorizedResponse(
                        self.credentialFactories,
                        request.remoteAddr)))

    def locateChild(self, request, seg):
        """
        Authenticate the request then return the C{self.wrappedResource}
        and the unmodified segments.
        We're not using path location, we want to fall back to the renderHTTP() call.
        """
        #return self.authenticate(request), seg
        return self, server.StopTraversal

    def renderHTTP(self, request):
        """
        Authenticate the request then return the result of calling renderHTTP
        on C{self.wrappedResource}
        """
        if request.method not in self.allowedMethods():
            response = http.Response(responsecode.NOT_ALLOWED)
            response.headers.setHeader("allow", self.allowedMethods())
            return response

        def _renderResource(resource):
            return resource.renderHTTP(request)

        def _finished_reading(ignore, result):
            data = ''.join(result)
            request.attachment = data
            d = self.authenticate(request)
            d.addCallback(_renderResource)
            return d

        if request.method in ('PUT', 'DELETE'):
            # we need to authenticate the request after all the attachment stream
            # has been read
            # QQQ DELETE doesn't have any attachments, does it? nor does GET.
            # QQQ Reading attachment when there isn't one won't hurt, will it?
            # QQQ So why don't we just do it all the time for all requests?
            data = []
            d = stream.readStream(request.stream, data.append)
            d.addCallback(_finished_reading, data)
            return d
        else:
            d = self.authenticate(request)
            d.addCallback(_renderResource)

        return d
