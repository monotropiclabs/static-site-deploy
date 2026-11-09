// Setup replaces the two JSON literals below before sending this to CloudFormation.
var canonicalDomain = __CANONICAL_DOMAIN__;
var redirectDomains = __REDIRECT_DOMAINS__;

function querySuffix(query) {
    var pairs = [];
    Object.keys(query || {}).forEach(function (key) {
        var item = query[key];
        (item.multiValue || [item]).forEach(function (entry) {
            pairs.push(key + '=' + entry.value);
        });
    });
    return pairs.length ? '?' + pairs.join('&') : '';
}

function redirect(location) {
    return {
        statusCode: 301,
        statusDescription: 'Moved Permanently',
        headers: {
            location: { value: location },
            'cache-control': { value: 'public, max-age=3600' }
        }
    };
}

function handler(event) {
    var request = event.request;
    var uri = request.uri;
    var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
    var directoryWithoutSlash = !uri.endsWith('/') && lastSegment.indexOf('.') === -1;
    var directoryUri = directoryWithoutSlash ? uri + '/' : uri;
    var query = querySuffix(request.querystring);

    if (redirectDomains.indexOf(request.headers.host.value) !== -1) {
        return redirect('https://' + canonicalDomain + directoryUri + query);
    }
    if (directoryWithoutSlash) {
        return redirect(directoryUri + query);
    }
    if (uri.endsWith('/')) {
        request.uri += 'index.html';
    }
    return request;
}
