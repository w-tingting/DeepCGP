import numpy as np
import tensorflow as tf
import gpflow
from gpflow import settings, features, transforms
from gpflow.kullback_leiblers import gauss_kl
from doubly_stochastic_dgp.layers import Layer
from kernels import PatchInducingFeature
from conditionals import base_conditional
from views import FullView

class MultiOutputConvKernel(gpflow.kernels.Kernel):
    def __init__(self, base_kernel, input_dim, patch_count):
        super().__init__(input_dim=input_dim)
        self.base_kernel = base_kernel
        self.patch_count = patch_count

    def Kuu(self, ML_Z):
        M = tf.shape(ML_Z)[0]
        return self.base_kernel.K(ML_Z) + tf.eye(M,
                dtype=settings.float_type) * settings.jitter

    def Kuf(self, ML_Z, PNL_patches):
        """ Returns covariance between inducing points and input.
        Output shape: patch_count x M x N
        """
        def patch_covariance(NL_patches):
            # Returns covariance matrix of size M x N.
            return self.base_kernel.K(ML_Z, NL_patches)

        PMN_Kzx = tf.map_fn(patch_covariance, PNL_patches, parallel_iterations=self.patch_count)
        return PMN_Kzx

    def Kff(self, PNL_patches):
        """Kff returns auto covariance of the input.
        :return: O (== P) x N x N covariance matrices.
        """
        def patch_auto_covariance(NL_patches):
            # Returns covariance matrix of size N x N.
            return self.base_kernel.K(NL_patches)
        return tf.map_fn(patch_auto_covariance, PNL_patches, parallel_iterations=self.patch_count)

    def Kdiag(self, PNL_patches):
        """
        :return: O X N diagonals of the covariance matrices.
        """
        def Kdiag(NL_patch):
            ":return: N diagonal of covariance matrix."
            return self.base_kernel.Kdiag(NL_patch)
        return tf.map_fn(Kdiag, PNL_patches, parallel_iterations=self.patch_count)

class ConvLayer(Layer):
    def __init__(self, base_kernel, mean_function, feature=None, view=None,
            white=False,
            gp_count=1,
            **kwargs):
        super().__init__(**kwargs)
        self.base_kernel = base_kernel

        self.view = view

        self.feature_maps_in = self.view.feature_maps
        self.gp_count = gp_count

        self.patch_count = self.view.patch_count
        self.patch_length = self.view.patch_length
        self.num_outputs = self.patch_count * gp_count

        self.conv_kernel = MultiOutputConvKernel(base_kernel,
                np.prod(view.input_size) * view.feature_maps, patch_count=self.patch_count)


        self.white = white

        self.feature = feature

        self.num_inducing = len(feature)

        q_mu = np.zeros((self.num_inducing, self.gp_count)).astype(settings.float_type)
        self.q_mu = gpflow.Param(q_mu)

        #TODO figure out if we need whitened vs non-whitened GP.
        if not self.white:
            GMM_q_sqrt = self._init_q_S()
        else:
            GMM_q_sqrt = np.tile(np.eye(self.num_inducing, dtype=settings.float_type)[None, :, :], [gp_count, 1, 1])
        q_sqrt_transform = gpflow.transforms.LowerTriangular(self.num_inducing, num_matrices=self.gp_count)
        self.q_sqrt = gpflow.Param(GMM_q_sqrt, transform=q_sqrt_transform)

        self.mean_function = mean_function
        self._build_prior_cholesky()

    def conditional_ND(self, ND_X, full_cov=False):
        """
        Returns the mean and the variance of q(f|m, S) = N(f| Am, K_nn + A(S - K_mm)A^T)
        where A = K_nm @ K_mm^{-1}

        dimension O: num_outputs (== patch_count * gp_count)

        :param ON_X: The input X of shape O x N
        :param full_cov: if true, var is in (N, N, D_out) instead of (N, D_out) i.e. we
        also compute entries outside the diagonal.
        """
        N = tf.shape(ND_X)[0]
        NHWC_X = tf.reshape(ND_X, [N, self.view.input_size[0], self.view.input_size[1], self.feature_maps_in])
        PNL_patches = self.view.extract_patches_PNL(NHWC_X)

        MM_Kuu = self.conv_kernel.Kuu(self.feature.Z)
        PMN_Kuf = self.conv_kernel.Kuf(self.feature.Z, PNL_patches)

        if full_cov:
            P_Knn = self.conv_kernel.Kff(PNL_patches)
        else:
            P_Knn = self.conv_kernel.Kdiag(PNL_patches)

        Lm = tf.cholesky(MM_Kuu)

        def conditional(tupled):
            MN_Kuf, Knn = tupled
            return base_conditional(MN_Kuf, Lm, Knn, self.q_mu, full_cov=full_cov, q_sqrt=self.q_sqrt, white=self.white)

        PNG_mean, var = tf.map_fn(conditional, (PMN_Kuf, P_Knn), (settings.float_type, settings.float_type),
                parallel_iterations=self.patch_count)

        NO_mean = tf.reshape(tf.transpose(PNG_mean, [1, 0, 2]), [N, self.gp_count * self.patch_count])

        if full_cov:
            # var: P x G x N x N
            ONN_var = tf.reshape(var, [self.patch_count * self.gp_count, N, N])
            var = tf.transpose(ONN_var[:, :, :], [2, 1, 0])
        else:
            # var: P x N x G
            var = tf.transpose(var, [1, 0, 2])
            NO_var = tf.reshape(var, [N, self.patch_count * self.gp_count])

        mean_view = self.view.mean_view(NHWC_X, PNL_patches)
        mean = NO_mean + self.mean_function(mean_view)
        return mean, var

    def KL(self):
        """
        The KL divergence from the variational distribution to the prior.
        q ~ N(\mu, S)

        :return: KL divergence from q(u) = N(q_mu, q_s) to p(u) ~ N(0, Kuu), independently for each GP
        """
        if self.white:
            return gauss_kl(self.q_mu, self.q_sqrt, K=None)
        else:
            return gauss_kl(self.q_mu, self.q_sqrt, self.MM_Ku_prior)

    def _build_prior_cholesky(self):
        self.MM_Ku_prior = self.conv_kernel.Kuu(self.feature.Z.read_value())
        MM_Lu_prior = tf.linalg.cholesky(self.MM_Ku_prior)
        self.MM_Lu_prior = self.enquire_session().run(MM_Lu_prior)

    def _init_q_S(self):
        MM_Ku = self.conv_kernel.Kuu(self.feature.Z.read_value())
        MM_Lu = tf.linalg.cholesky(MM_Ku)
        MM_Lu = self.enquire_session().run(MM_Lu)
        return np.tile(MM_Lu[None, :, :], [self.gp_count, 1, 1])


