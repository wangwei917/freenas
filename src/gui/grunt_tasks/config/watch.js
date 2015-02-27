// WATCH
// Uses native filesystem methods to watch for changes in given directories,
// and executes the associated commands when a change is detected. In this
// case, it's a two-step process, where the 'bundle' created by Browserify
// is also watched in order to trigger a live reload.

"use strict";

module.exports = function( grunt ) {
  // BUILD WORLD
  // Rebuild Browserify bundle when source JS/JSX changes
  this.app = {
      files: [
          "<%= dirTree.source.jsx %>/**"
        , "<%= dirTree.routes %>.js"
      ]
    , tasks: [ "browserify:app" ]
  };

  // Rebuild CSS when LESS files change
  this.less = {
      files: [ "<%= dirTree.source.styles %>/**" ]
    , tasks: [ "less:core" ]
  };

  // Copy new/updated images into build
  this.images = {
      files: [ "<%= dirTree.source.images %>/**" ]
    , tasks: [ "copy:images" ]
  };


  // SERVER LIFECYCLE
  // Run local express task, restart when
  this.localServer = {
      files: [
          "<%= dirTree.client %>.js"
        , "<%= dirTree.routes %>.js"
        , "<%= dirTree.server %>.js"
        , "<%= dirTree.source.templates %>/**"
      ]
    , tasks: [ "express:devServer" ]
  };

  // Restarts Forever process on remote FreeNAS when server or client change
  var serverWatchFiles = [
      "<%= dirTree.client %>.js"
    , "<%= dirTree.server %>.js"
    , "<%= dirTree.source.templates %>/**"
    , "<%= dirTree.build.root %>/**"
    , "package.json"
    , "bower_components/**"
  ];
  this["freenasServer"] = {
      files: serverWatchFiles
    , tasks: [ "freenas-config:silent", "rsync", "ssh-multi-exec:start-server" ]
  };
};