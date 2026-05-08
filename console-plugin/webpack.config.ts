import * as path from 'path';
import * as webpack from 'webpack';
import { ConsoleRemotePlugin } from '@openshift-console/dynamic-plugin-sdk-webpack';
import pluginMetadata from './plugin-metadata';

const fixWindowsDynamicPatternFlyRequests = new webpack.NormalModuleReplacementPlugin(
  /^(@patternfly\/(?:react-core|react-icons|react-table))\/distdynamic(components|layouts|icons)(.+)$/,
  (resource) => {
    const match = resource.request.match(
      /^(@patternfly\/(?:react-core|react-icons|react-table))\/distdynamic(components|layouts|icons)(.+)$/,
    );
    if (match) {
      resource.request = `${match[1]}/dist/dynamic/${match[2]}/${match[3]}`;
    }
  },
);

const config = {
  mode: 'development' as const,
  entry: {},
  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: '[name]-bundle.js',
    chunkFilename: '[name]-chunk.js',
  },
  resolve: {
    extensions: ['.ts', '.tsx', '.js', '.jsx'],
  },
  module: {
    rules: [
      {
        test: /\.(ts|tsx)$/,
        exclude: /node_modules/,
        use: [
          {
            loader: 'ts-loader',
            options: { transpileOnly: true },
          },
        ],
      },
      {
        test: /\.s?css$/,
        use: ['style-loader', 'css-loader', 'sass-loader'],
      },
    ],
  },
  plugins: [
    fixWindowsDynamicPatternFlyRequests,
    new ConsoleRemotePlugin({ pluginMetadata }),
  ],
  devServer: {
    port: 9001,
    allowedHosts: 'all',
    client: {
      overlay: {
        errors: true,
        warnings: false,
      },
    },
    static: path.join(__dirname, 'dist'),
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
      'Access-Control-Allow-Headers': 'X-Requested-With, Content-Type, Authorization',
    },
    devMiddleware: {
      writeToDisk: true,
    },
    proxy: [
      {
        context: ['/api/v1/diagnose', '/api/v1/health', '/api/v1/providers'],
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    ],
  },
  devtool: 'source-map' as const,
};

export default config;
